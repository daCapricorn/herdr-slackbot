"""Plugin lifecycle entrypoint; same registration protocol, agent-specific evidence."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .agent_adapters import foreground_claude_session
from .claude_sync import resolve_claude, claude_home, SESSION_ID
from .session_hook import HookTransport, caller_pids, report_session
from .herdr_client import HerdrClient
from .session_observation import save_observation


def report_claude(payload, env, client, *, home=None, seq=None, pids=None):
    if env.get("HERDR_ENV") != "1" or not env.get("HERDR_SOCKET_PATH"):
        return False
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in ("SessionStart", "UserPromptSubmit"):
        return False
    if payload.get("hook_event_name") == "SessionStart" and payload.get("source") not in ("startup", "resume", "clear", "compact"):
        return False
    pids = caller_pids() if pids is None else pids
    candidates = []
    for info in client.list_agents():
        if info.get("agent") != "claude" or info.get("cwd") != payload.get("cwd"):
            continue
        process = client.process_info(info["pane_id"])
        if not any(p.get("pid") in pids for p in process.get("foreground_processes", [])):
            continue
        tid = foreground_claude_session(info, process, home=home)
        if tid and tid == payload.get("session_id"):
            candidates.append((info, process))
    if len(candidates) != 1:
        return False
    info, process = candidates[0]
    live = client.get_agent(info["pane_id"])
    if (any(live.get(k) != info.get(k) for k in ("pane_id", "terminal_id", "cwd", "agent"))
            or client.process_info(info["pane_id"]) != process
            or foreground_claude_session(info, process, home=home) != payload["session_id"]):
        return False
    session = resolve_claude({**info, "agent_session": {"value": payload["session_id"]}}, home=home, require_rollout=False)
    seq = time.time_ns() if seq is None else seq
    # Lifecycle hooks run before the new prompt is appended. A nonexistent new
    # transcript gets a terminal/session-bound zero cutoff, not a history scan.
    offset = session[1].stat().st_size if session[1].exists() else 0
    if not save_observation(Path(home or claude_home()), info, session, seq, offset, allow_missing=True):
        return False
    params = {"pane_id": info["pane_id"], "source": "herdr:claude", "agent": "claude",
              "seq": seq, "agent_session_id": payload["session_id"]}
    if payload.get("hook_event_name") == "SessionStart":
        source = payload.get("source")
        if source not in ("startup", "resume", "clear", "compact"):
            return False
        params["session_start_source"] = source
    client.request("pane.report_agent_session", params)
    return True


def main():
    seq = time.time_ns()
    if os.name != "nt":
        os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=("codex", "claude"), required=True)
    agent = parser.parse_args().agent
    payload, reported, error = {}, False, None
    try:
        payload = json.loads(sys.stdin.read(1024 * 1024))
        socket = os.environ.get("HERDR_SOCKET_PATH")
        if not socket:
            return
        client = HerdrClient(HookTransport(socket, connect_timeout=0.5))
        handler = report_session if agent == "codex" else report_claude
        reported = handler(payload, os.environ, client, seq=seq)
    except Exception as exc:
        error = type(exc).__name__
    finally:
        # Private evidence for plugin execution, without prompt text or secrets.
        try:
            tid = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(tid, str) and SESSION_ID.fullmatch(tid):
                home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") if agent == "codex" else claude_home()
                root = home / "herdr-bridge/hooks"
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = root / (tid + "-plugin.json")
                path.write_text(json.dumps({"agent": agent, "session_id": tid, "event": payload.get("hook_event_name"),
                                            "reported": reported, "error": error, "seq": seq}) + "\n")
                path.chmod(0o600)
        except Exception:
            pass  # Never let diagnostics block or steer the agent.



if __name__ == "__main__":
    main()
