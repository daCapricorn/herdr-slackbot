"""Claude adapter: exact root transcripts, stable message IDs and final answers."""
import hashlib
import json
import os
import re
from pathlib import Path

from .events import session_of
from .claude_records import turn_start
from .codex_session import SessionUnavailable
from .codex_sync import _redact
from .transcript_sync import TranscriptSync

SESSION_ID = re.compile(r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def resolve_claude(info, *, home=None, thread_id=None, require_rollout=True):
    home = Path(home or claude_home())
    native, cwd = session_of(info), info.get("cwd")
    if info.get("agent") != "claude" or not cwd:
        raise SessionUnavailable("Claude workspace unavailable")
    if native and thread_id and native != thread_id:
        raise SessionUnavailable("Bound session differs from Herdr session")
    tid = native or thread_id
    if not isinstance(tid, str) or not SESSION_ID.fullmatch(tid):
        raise SessionUnavailable("Exact Claude session id required; no directory guessing")
    paths = list((home / "projects").glob(f"*/{tid}.jsonl"))
    if len(paths) > 1:
        raise SessionUnavailable("Claude transcript match ambiguous")
    path = paths[0] if paths else home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd.rstrip("/\\")) / (tid + ".jsonl")
    if not require_rollout and not paths:
        return tid, path, cwd
    try:
        matched = False
        with path.open("rb") as stream:
            # Root identity must exist in the transcript, not merely its filename.
            for _ in range(512):
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("isSidechain"):
                    continue
                if row.get("sessionId") and row.get("cwd"):
                    if row["sessionId"] != tid or row["cwd"] != cwd:
                        raise SessionUnavailable("Claude transcript identity differs from pane")
                    matched = True
                    break
        if not matched:
            raise SessionUnavailable("Claude root transcript identity unavailable")
    except OSError as exc:
        raise SessionUnavailable("Claude transcript unavailable; waiting for first prompt") from exc
    return tid, path, cwd


class ClaudeTranscriptSync(TranscriptSync):
    agent_kind = "claude"
    namespace = "claude"

    def __init__(self, db, *, home=None):
        super().__init__(db, home=home or claude_home())
        self.db.execute("CREATE TABLE IF NOT EXISTS claude_sync_pending (source_key TEXT PRIMARY KEY, record TEXT NOT NULL)")

    def _resolve(self, info, unique_workspace=False):
        if session_of(info):
            return resolve_claude(info, home=self.home)
        row = info["_bridge_binding"] if "_bridge_binding" in info else self.binding_snapshot(info)
        if row and row[1] != info.get("cwd"):
            raise SessionUnavailable("Bound directory changed; bind the current session again")
        return resolve_claude(info, home=self.home, thread_id=row[0] if row else None)

    def receipt_baseline(self, info):
        """Allow the exact first-turn path only when it does not exist yet."""
        try:
            return super().receipt_baseline(info)
        except SessionUnavailable:
            tid, path, _ = resolve_claude(info, home=self.home, require_rollout=False)
            if path.exists():
                raise
            return {"source_key": f"{info.get('terminal_id') or info.get('pane_id', '')}:{tid}",
                    "path": str(path), "device": None, "inode": None,
                    "offset": 0, "missing": True}

    def validate_binding(self, info, thread_id):
        if not info.get("terminal_id"):
            raise SessionUnavailable("Exact terminal id required for binding")
        return resolve_claude(info, home=self.home, thread_id=thread_id)

    def apply_binding(self, info, session):
        tid, _, cwd = session
        self.db.execute("INSERT OR REPLACE INTO claude_session_bindings VALUES (?,?,?)", (info["terminal_id"], tid, cwd))
        self._status(info, "ready", thread_id=tid, source="explicit", reason="Bound; waiting for next scan")
        return self.diagnostic(info)

    def bind(self, info, thread_id):
        return self.apply_binding(info, self.validate_binding(info, thread_id))

    def binding_snapshot(self, info):
        return self.db.execute("SELECT thread_id,cwd FROM claude_session_bindings WHERE terminal_id=?", (info.get("terminal_id"),)).fetchone()

    def bound_info(self, info, *, binding=...):
        if binding is ...:
            binding = self.binding_snapshot(info)
        if not binding:
            return info
        tid, _, _ = self._resolve({**info, "_bridge_binding": binding})
        return {**info, "agent_session": {"value": tid}}

    def latest_turn_offset(self, path):
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - 16 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            latest = size
            typed = None
            settled = False
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("isSidechain") or row.get("isMeta"):
                    continue
                if row.get("type") == "system" and row.get("subtype") == "turn_duration":
                    settled = True
                    continue
                if boundary := turn_start(row):
                    if settled:
                        typed = None
                    settled = False
                    latest = stream.tell() - len(line)
                    if row.get("type") == "user" and boundary[0] == "user":
                        typed = latest
        return typed if typed is not None else latest

    @staticmethod
    def event_id(source_key, item_id, role):
        return hashlib.sha256(f"claude|{source_key}|{role}|{item_id}".encode()).hexdigest()

    def _flush(self, key):
        row = self.db.execute("SELECT record FROM claude_sync_pending WHERE source_key=?", (key,)).fetchone()
        if not row:
            return []
        record = json.loads(row[0])
        self.db.execute("DELETE FROM claude_sync_pending WHERE source_key=?", (key,))
        text = "\n".join(record["texts"].values()).strip()
        if not text or record.get("stop_reason") not in ("end_turn", "stop_sequence", "max_tokens"):
            return []
        event_id = self.event_id(key, record["id"], "assistant")
        info = record.get("completion_info") or {}
        completion = {"event_id": event_id, "source_key": key,
            "terminal_id": info.get("terminal_id") or info.get("pane_id") or key.rsplit(":", 1)[0],
            "session_id": key.rsplit(":", 1)[-1], "turn_id": record["id"], "text": text,
            "info": info, "agent": record.get("completion_agent"),
            "source": info.get("_transcript_source"),
            "legacy": "completion_info" not in record}
        return [(event_id, {"role": "assistant", "text": text, "_completion": completion})]

    def row_events(self, source_key, row, offset, *, info=None, agent=None):
        tid = source_key.rsplit(":", 1)[-1]
        if row.get("isSidechain") or row.get("isMeta"):
            return []
        if row.get("sessionId") and row["sessionId"] != tid:
            raise SessionUnavailable("Claude transcript changed session while reading")
        events = []
        start = turn_start(row)
        if start:
            events.extend(self._flush(source_key))
            role, text = start
            if role == "user":
                events.append((self.event_id(source_key, row.get("uuid") or str(offset), "user"),
                               {"role": "user", "text": text}))
        elif row.get("type") == "system" and row.get("subtype") == "turn_duration":
            events.extend(self._flush(source_key))
        elif row.get("type") == "assistant":
            message = row.get("message", {})
            if not isinstance(message, dict):
                return []
            mid = message.get("id")
            if not isinstance(mid, str) or not mid:
                return []
            old = self.db.execute("SELECT record FROM claude_sync_pending WHERE source_key=?", (source_key,)).fetchone()
            pending = json.loads(old[0]) if old else {"id": mid, "texts": {}}
            if pending["id"] != mid:
                events.extend(self._flush(source_key))
                pending = {"id": mid, "texts": {}}
            if "completion_info" not in pending:
                pending["completion_info"] = info or {}
                pending["completion_agent"] = agent
            pending["stop_reason"] = message.get("stop_reason") or pending.get("stop_reason")
            content = message.get("content", [])
            for index, block in enumerate(content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    pending["texts"][f"{row.get('uuid') or offset}:{index}"] = block["text"]
                elif block.get("type") == "tool_use":
                    name, tool_id = block.get("name", "tool"), block.get("id")
                    if not tool_id:
                        continue
                    args = block.get("input", {})
                    command = args.get("command") if isinstance(args, dict) and name == "Bash" else None
                    summary = "Used: " + str(name)[:100]
                    if isinstance(command, str):
                        command = " ".join(_redact(command).split())
                        summary = "Ran: `" + command[:220].replace("`", "'") + ("…" if len(command) > 220 else "") + "`"
                    events.append((self.event_id(source_key, tool_id, "tool"), {"role": "tool", "text": summary}))
            self.db.execute("INSERT OR REPLACE INTO claude_sync_pending VALUES (?,?)", (source_key, json.dumps(pending)))
        return events

    def receipt_user_texts(self, row, offset):
        if row.get("isSidechain") or row.get("isMeta"):
            return ()
        start = turn_start(row)
        return (start[1],) if start and start[0] == "user" else ()

    def finish_events(self, source_key, info, complete):
        # Herdr idle can precede the last transcript append. Only a durable
        # turn-duration boundary or next genuine prompt commits the answer.
        return []

    def reset_source(self, source_key, path=None):
        self.db.execute("DELETE FROM claude_sync_pending WHERE source_key=?", (source_key,))
