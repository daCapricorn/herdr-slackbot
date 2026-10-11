"""Incremental, read-only Codex rollout reader for pane-channel conversation sync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .codex_session import resolve_session, validate_rollout, SessionUnavailable


def _latest_turn_offset(path: Path, limit: int = 16 * 1024 * 1024) -> int:
    """Find the newest task start near EOF so first attach skips old history."""
    size = path.stat().st_size
    start = max(0, size - limit)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    if start:
        newline = data.find(b"\n")
        if newline < 0:
            return size
        data = data[newline + 1:]
        base = start + newline + 1
    else:
        base = 0
    cursor = base
    latest = None
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        try:
            row = json.loads(line)
            payload = row.get("payload", {})
            if row.get("type") == "event_msg" and payload.get("type") in ("task_started", "turn_started"):
                latest = cursor
        except (ValueError, AttributeError):
            pass
        cursor += len(line)
    return latest if latest is not None else size


def _text_from_content(content, accepted=("input_text", "text")):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in accepted and isinstance(part.get("text"), str):
            pieces.append(part["text"])
        elif part.get("type") in ("input_image", "image"):
            pieces.append("[image]")
    return "\n".join(x.strip() for x in pieces if x.strip()).strip()


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(\b[A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API[_-]?KEY)\b\s*[=:]\s*)([^\s,;]+)", r"\1[redacted]", text)
    text = re.sub(r"(?i)(\b(?:authorization\s*[:=]\s*(?:bearer|basic)|--(?:token|password|secret|api-key)(?:=|\s+)))([^\s,;]+)", r"\1[redacted]", text)
    text = re.sub(r"\b(?:xox[baprs]-|sk-[A-Za-z0-9_-]{12,})[A-Za-z0-9_-]*", "[redacted]", text)
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[redacted]", text)
    return text


def _tool_summary(item):
    kind = str(item.get("type", "Tool")).replace("Call", " call")
    if kind == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = " ".join(_redact(str(command or "")).split())
        if not command:
            return "Ran a command"
        command = command[:220] + ("…" if len(command) > 220 else "")
        return "Ran: `" + command.replace("`", "'") + "`"
    if kind in ("McpToolCall", "DynamicToolCall"):
        label = ".".join(str(v) for v in (item.get("server") or item.get("namespace"), item.get("tool")) if v)
        return "Used: " + (label or "tool")
    if kind in ("WebSearch", "WebSearchCall"):
        return "Searched the web"
    if kind in ("CollabAgentToolCall", "CollabAgentTool"):
        return "Used Codex subagent"
    if kind in ("AgentMessage", "UserMessage", "Reasoning", "ReasoningItem", "Plan",
                "FunctionCallOutput", "CustomToolCallOutput"):
        return None
    return "Used: " + kind[:100]


def _completed_agent_message(item):
    if item.get("type") not in ("AgentMessage", "agent_message"):
        return None
    if item.get("delivery") == "async":
        return None
    phase = item.get("phase")
    if phase and phase not in ("final_answer", "finalAnswer"):
        return None
    text = item.get("text", item.get("message"))
    if text is None:
        text = _text_from_content(item.get("content"), accepted=("output_text", "text"))
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def _user_item(item):
    if item.get("type") not in ("UserMessage", "user_message"):
        return None
    content = item.get("content")
    if isinstance(content, str):
        text = content.strip()
    else:
        text = _text_from_content(content, accepted=("text", "input_text"))
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text).strip()
    return _clean_user_text(text)


def _clean_user_text(text):
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text).strip()
    if not text or text.startswith("<send_user_message_question_reply>"):
        return None
    return text


def _stable_id(source_key, row, offset, role, text):
    payload = row.get("payload", {})
    item = payload.get("item", {}) if isinstance(payload, dict) else {}
    item_id = (item.get("id") if isinstance(item, dict) else None) or payload.get("id")
    row_key = item_id or row.get("ordinal") or row.get("timestamp") or f"{offset}:{hashlib.sha256(text.encode()).hexdigest()}"
    return hashlib.sha256(f"{source_key}|{role}|{row_key}".encode()).hexdigest()


class CodexRolloutSync:
    def __init__(self, db, *, home=None):
        self.db = db
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.db.execute("""CREATE TABLE IF NOT EXISTS codex_sync_cursors
            (source_key TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, device INTEGER NOT NULL,
             inode INTEGER NOT NULL, offset INTEGER NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS codex_sync_seen (event_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS codex_session_bindings (terminal_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, cwd TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS codex_sync_status (terminal_id TEXT PRIMARY KEY, record TEXT NOT NULL)")

    def diagnostic(self, info):
        key = info.get("terminal_id") or info.get("pane_id", "")
        row = self.db.execute("SELECT record FROM codex_sync_status WHERE terminal_id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {"state": "pending", "reason": "Not scanned yet"}

    def _status(self, info, state, **fields):
        key = info.get("terminal_id") or info.get("pane_id", "")
        self.db.execute("INSERT OR REPLACE INTO codex_sync_status VALUES (?, ?)",
                        (key, json.dumps({"state": state, **fields})))

    def _resolve(self, info, unique_workspace=False):
        key = info.get("terminal_id")
        binding = self.db.execute("SELECT thread_id,cwd FROM codex_session_bindings WHERE terminal_id=?", (key,)).fetchone()
        if binding and binding[1] != info.get("cwd"):
            raise SessionUnavailable("Bound directory changed; bind the current session again")
        return validate_rollout(resolve_session(info, home=self.home,
            unique_workspace=unique_workspace, thread_id=binding[0] if binding else None))

    def bound_info(self, info):
        binding = self.db.execute("SELECT thread_id FROM codex_session_bindings WHERE terminal_id=?",
                                  (info.get("terminal_id"),)).fetchone()
        if not binding:
            return info
        tid, _, _ = self._resolve(info)
        return {**info, "agent_session": {"value": tid}}

    def bind(self, info, thread_id):
        if not info.get("terminal_id"):
            raise SessionUnavailable("Exact terminal id required for binding")
        tid, path, cwd = validate_rollout(resolve_session(info, home=self.home, thread_id=thread_id))
        self.db.execute("INSERT OR REPLACE INTO codex_session_bindings VALUES (?, ?, ?)",
                        (info["terminal_id"], tid, cwd))
        self._status(info, "ready", thread_id=tid, source="explicit", reason="Bound; waiting for next scan")
        return self.diagnostic(info)

    def sync(self, info, enqueue, *, unique_workspace=False):
        try:
            thread_id, path, _ = self._resolve(info, unique_workspace)
        except SessionUnavailable as exc:
            self._status(info, "unavailable", reason=str(exc))
            return 0
        try:
            stat = path.stat()
            terminal = info.get("terminal_id") or info.get("pane_id", "")
            source_key = f"{terminal}:{thread_id}"
            row = self.db.execute("SELECT rollout_path,device,inode,offset FROM codex_sync_cursors WHERE source_key=?",
                                  (source_key,)).fetchone()
            same_file = row and row[0] == str(path) and row[1] == stat.st_dev and row[2] == stat.st_ino
            if same_file and stat.st_size >= row[3]:
                offset = row[3]
            elif info.get("agent_status") in ("working", "blocked"):
                offset = _latest_turn_offset(path)
            else:
                offset = stat.st_size
            cursor = offset
            with path.open("rb") as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    next_cursor = stream.tell()
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise ValueError("invalid rollout row")
                    except (ValueError, UnicodeDecodeError):
                        cursor = next_cursor
                        continue
                    payload = raw.get("payload", {})
                    event = None
                    if raw.get("type") == "event_msg" and isinstance(payload, dict):
                        if payload.get("type") == "user_message":
                            text = _clean_user_text(str(payload.get("message") or ""))
                            if text:
                                event = {"role": "user", "text": text}
                        elif payload.get("type") == "item_completed":
                            item = payload.get("item", {})
                            if isinstance(item, dict):
                                text = _user_item(item)
                                if text:
                                    event = {"role": "user", "text": text}
                                text = _completed_agent_message(item)
                                if text:
                                    event = {"role": "assistant", "text": text}
                                elif not event:
                                    summary = _tool_summary(item)
                                    if summary:
                                        event = {"role": "tool", "text": summary}
                    if event:
                        event_id = _stable_id(source_key, raw, cursor, event["role"], event["text"])
                        inserted = self.db.execute("INSERT OR IGNORE INTO codex_sync_seen VALUES (?)",
                                                   (event_id,)).rowcount
                        if inserted:
                            enqueue(event_id, info, event)
                    cursor = next_cursor
                    self.db.execute("""INSERT INTO codex_sync_cursors
                        (source_key,rollout_path,device,inode,offset) VALUES (?,?,?,?,?)
                        ON CONFLICT(source_key) DO UPDATE SET rollout_path=excluded.rollout_path,
                        device=excluded.device,inode=excluded.inode,offset=excluded.offset""",
                        (source_key, str(path), stat.st_dev, stat.st_ino, cursor))
            # Empty/settled rollouts also need a baseline cursor before the next turn.
            self.db.execute("""INSERT OR REPLACE INTO codex_sync_cursors
                (source_key,rollout_path,device,inode,offset) VALUES (?,?,?,?,?)""",
                (source_key, str(path), stat.st_dev, stat.st_ino, cursor))
            self._status(info, "syncing", thread_id=thread_id, offset=cursor)
            return cursor - offset
        except (OSError, sqlite3.Error) as exc:
            self._status(info, "unavailable", reason=f"Rollout read failed: {type(exc).__name__}")
            return 0
