"""Claude adapter: exact root transcripts, stable message IDs and final answers."""
import hashlib
import json
import os
import re
from pathlib import Path

from .events import session_of
from .codex_session import SessionUnavailable
from .codex_sync import _redact
from .transcript_sync import TranscriptSync

SESSION_ID = re.compile(r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def prompt_text(row):
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") if b.get("type") == "text" else "[image]"
                         for b in content if isinstance(b, dict) and b.get("type") in ("text", "image")).strip()
    else:
        return None
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text).strip()
    if not text or text.startswith(("[Request interrupted", "<command-", "<local-command", "<send_user_message_question_reply>")):
        return None
    return text


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
        row = self.db.execute("SELECT thread_id,cwd FROM claude_session_bindings WHERE terminal_id=?",
                              (info.get("terminal_id"),)).fetchone()
        if row and row[1] != info.get("cwd"):
            raise SessionUnavailable("Bound directory changed; bind the current session again")
        return resolve_claude(info, home=self.home, thread_id=row[0] if row else None)

    def bind(self, info, thread_id):
        if not info.get("terminal_id"):
            raise SessionUnavailable("Exact terminal id required for binding")
        tid, _, cwd = resolve_claude(info, home=self.home, thread_id=thread_id)
        self.db.execute("INSERT OR REPLACE INTO claude_session_bindings VALUES (?,?,?)", (info["terminal_id"], tid, cwd))
        self._status(info, "ready", thread_id=tid, source="explicit", reason="Bound; waiting for next scan")
        return self.diagnostic(info)

    def bound_info(self, info):
        row = self.db.execute("SELECT thread_id FROM claude_session_bindings WHERE terminal_id=?", (info.get("terminal_id"),)).fetchone()
        if not row:
            return info
        tid, _, _ = self._resolve(info)
        return {**info, "agent_session": {"value": tid}}

    def latest_turn_offset(self, path):
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - 16 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            latest = size
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and prompt_text(row):
                        latest = stream.tell() - len(line)
                except (ValueError, AttributeError, TypeError):
                    continue
        return latest

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
        return [(self.event_id(key, record["id"], "assistant"), {"role": "assistant", "text": text})]

    def row_events(self, source_key, row, offset):
        tid = source_key.rsplit(":", 1)[-1]
        if row.get("isSidechain") or row.get("isMeta"):
            return []
        if row.get("sessionId") and row["sessionId"] != tid:
            raise SessionUnavailable("Claude transcript changed session while reading")
        events = []
        prompt = prompt_text(row)
        if prompt:
            events.extend(self._flush(source_key))
            events.append((self.event_id(source_key, row.get("uuid") or str(offset), "user"),
                           {"role": "user", "text": prompt}))
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
                # A different API message supersedes commentary from the previous one.
                pending = {"id": mid, "texts": {}}
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

    def finish_events(self, source_key, info, complete):
        # Herdr idle can precede the last transcript append. Only a durable
        # turn-duration boundary or next genuine prompt commits the answer.
        return []

    def reset_source(self, source_key):
        self.db.execute("DELETE FROM claude_sync_pending WHERE source_key=?", (source_key,))
