"""Report exact Codex lifecycle identity to Herdr; never submit terminal input."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .codex_session import resolve_session, validate_rollout, SessionUnavailable
from .herdr_client import HerdrClient, UnixSocketTransport
from .session_observation import save_observation


def caller_pids():
    import psutil
    return {p.pid for p in psutil.Process().parents()}


def caller_agent(client, cwd, pids):
    candidates = []
    for info in client.list_agents():
        if info.get("agent") != "codex" or info.get("cwd") != cwd:
            continue
        process = client.process_info(info["pane_id"])
        if process.get("pane_id") != info["pane_id"]:
            continue
        if any(p.get("pid") in pids and "--no-daemon" in p.get("argv", [])
               for p in process.get("foreground_processes", [])):
            candidates.append(info)
    if len(candidates) != 1:
        raise SessionUnavailable("Hook has no unique foreground ancestor; use Codex --no-daemon")
    return candidates[0]


class HookTransport(UnixSocketTransport):
    def _open(self):
        handle = super()._open()
        handle.connection.settimeout(0.5)
        return handle


def report_session(payload, env, client, *, home=None, seq=None, pids=None):
    if env.get("HERDR_ENV") != "1" or not env.get("HERDR_SOCKET_PATH"):
        return False
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in ("SessionStart", "UserPromptSubmit"):
        return False
    tid, cwd = payload.get("session_id"), payload.get("cwd")
    if not isinstance(tid, str) or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", tid):
        return False
    if not isinstance(cwd, str) or not cwd:
        return False
    # Nested agents inherit the parent pane. They must not replace its identity.
    if env.get("CODEX_THREAD_ID") and env["CODEX_THREAD_ID"] != tid:
        return False
    pids = caller_pids() if pids is None else pids
    # Daemon hooks inherit the daemon's original pane environment, not the TUI
    # that selected the thread. Only a foreground ancestor proves this binding.
    info = caller_agent(client, cwd, pids)
    pane = info["pane_id"]
    fresh = client.get_agent(pane)
    if any(fresh.get(k) != info.get(k) for k in ("pane_id", "terminal_id", "agent", "cwd")):
        return False
    info = fresh
    if info.get("agent") != "codex" or info.get("cwd") != cwd or not info.get("terminal_id"):
        return False
    # Root-only database lookup and header check exclude subagent sessions even
    # when a nested runtime does not preserve CODEX_THREAD_ID.
    session = validate_rollout(resolve_session({**info, "agent_session": None}, home=home, thread_id=tid))
    live = client.get_agent(pane)
    if any(live.get(k) != info.get(k) for k in ("pane_id", "terminal_id", "agent", "cwd")):
        return False
    process = client.process_info(pane)
    if not any(p.get("pid") in pids and "--no-daemon" in p.get("argv", [])
               for p in process.get("foreground_processes", [])):
        return False
    seq = seq if seq is not None else time.time_ns()
    params = {"pane_id": pane, "source": "herdr:codex", "agent": "codex",
              "seq": seq, "agent_session_id": tid}
    if payload.get("hook_event_name") == "SessionStart":
        source = payload.get("source")
        if source not in ("startup", "resume", "clear", "compact"):
            return False
        params["session_start_source"] = source
    path = session[1]
    offset = path.stat().st_size
    # Prompt hooks have an exact turn id. Include that turn if it has already
    # been journaled, without importing the last historical turn on resume.
    turn = payload.get("turn_id")
    if payload["hook_event_name"] == "UserPromptSubmit" and isinstance(turn, str) and turn:
        with path.open("rb") as stream:
            start = max(0, offset - 16 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            while line := stream.readline():
                try:
                    row = json.loads(line)
                    event = row.get("payload", {})
                    if (row.get("type") == "event_msg" and event.get("type") in ("task_started", "turn_started")
                            and event.get("turn_id") == turn):
                        offset = stream.tell() - len(line)
                        break
                except (ValueError, AttributeError, TypeError):
                    pass
    root = Path(home or env.get("CODEX_HOME") or Path.home() / ".codex")
    if not save_observation(root, info, session, seq, offset):
        return False
    # transcript_path may legitimately be null for paginated Codex histories.
    client.request("pane.report_agent_session", params)
    return True


def main():
    seq = time.time_ns()  # Capture before any potentially delayed validation.
    payload, reported, error, reason = {}, False, None, None
    try:
        payload = json.loads(sys.stdin.read(1024 * 1024))
        socket_path = os.environ.get("HERDR_SOCKET_PATH")
        if not socket_path:
            return
        # Bound timeouts; no CLI fallback, retries, prompts, keys or hook output.
        client = HerdrClient(HookTransport(socket_path, connect_timeout=0.5))
        reported = report_session(payload, os.environ, client, seq=seq)
    except Exception as exc:
        error = type(exc).__name__
        reason = str(exc) if isinstance(exc, SessionUnavailable) else None
    finally:
        # Local diagnostics exclude prompt text, transcript contents and credentials.
        # A missing pane environment is visible even when no report was possible.
        try:
            tid = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(tid, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", tid):
                root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "herdr-bridge/hooks"
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                record = {"event": payload.get("hook_event_name"), "thread_id": tid,
                          "payload_fields": sorted(payload),
                          "inherited_pane_id": os.environ.get("HERDR_PANE_ID"), "herdr_env": os.environ.get("HERDR_ENV") == "1",
                          "has_socket": bool(os.environ.get("HERDR_SOCKET_PATH")),
                          "inherited_thread_matches": not os.environ.get("CODEX_THREAD_ID") or os.environ["CODEX_THREAD_ID"] == tid,
                          "reported": reported, "error": error, "reason": reason, "seq": seq}
                path = root / (tid + ".json")
                path.write_text(json.dumps(record) + "\n")
                path.chmod(0o600)
        except Exception:
            pass  # Reporting/diagnostics must not block or steer the agent's turn.


if __name__ == "__main__":
    main()
