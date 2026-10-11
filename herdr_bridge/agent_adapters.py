"""Agent-specific identity/readers behind one Bridge-facing contract."""
import json
import time
from pathlib import Path

from .claude_sync import ClaudeTranscriptSync, SESSION_ID, claude_home
from .codex_sync import CodexRolloutSync


def foreground_claude_session(info, process, *, home=None, started=None):
    """Read only a foreground PID's registry; check PID reuse and terminal context."""
    if info.get("agent") != "claude" or process.get("pane_id") != info.get("pane_id"):
        return None
    if started is None:
        import psutil
        started = lambda pid: (time.ctime(psutil.Process(pid).create_time()),
                               time.asctime(time.gmtime(psutil.Process(pid).create_time())))
    candidates = []
    for proc in process.get("foreground_processes", []):
        pid = proc.get("pid")
        if type(pid) is not int or pid <= 0:
            continue
        try:
            record = json.loads((Path(home or claude_home()) / "sessions" / f"{pid}.json").read_text())
            tid = record.get("sessionId")
            if (record.get("pid") == pid and record.get("cwd") == info.get("cwd")
                    and record.get("kind") == "interactive" and record.get("procStart") in ((value,) if isinstance(value := started(pid), str) else value)
                    and isinstance(tid, str) and SESSION_ID.fullmatch(tid)):
                candidates.append(tid)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return candidates[0] if len(candidates) == 1 else None


class AgentAdapter:
    """Identity enrichment + durable transcript source; no Slack or terminal writes."""
    def __init__(self, source):
        self.source = source

    def enrich(self, info, client):
        return info


class ClaudeAdapter(AgentAdapter):
    def enrich(self, info, client):
        try:
            process = client.process_info(info["pane_id"])
            tid = foreground_claude_session(info, process, home=self.source.home)
            # Revalidate process and terminal after reading the registry.
            if tid:
                fresh = client.get_agent(info["pane_id"])
                check = client.process_info(info["pane_id"])
                if (all(fresh.get(k) == info.get(k) for k in ("pane_id", "terminal_id", "cwd", "agent"))
                        and check == process and foreground_claude_session(fresh, check, home=self.source.home) == tid):
                    return {**info, "agent_session": {"agent": "claude", "kind": "id", "source": "bridge:claude-pid", "value": tid}}
        except Exception:
            pass  # Absence/unsupported registry never licenses a directory guess.
        return info


def adapters(db):
    return {"codex": AgentAdapter(CodexRolloutSync(db)), "claude": ClaudeAdapter(ClaudeTranscriptSync(db))}
