"""Incremental, read-only Codex rollout reader for pane-channel conversation sync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .codex_session import resolve_session, validate_rollout, SessionUnavailable
from .events import session_of
from .transcript_sync import TranscriptSync


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
        text = _text_from_content(item.get("content"), accepted=("output_text", "text", "Text"))
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


class CodexRolloutSync(TranscriptSync):
    agent_kind = "codex"
    namespace = "codex"

    def __init__(self, db, *, home=None):
        super().__init__(db, home=home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    def latest_turn_offset(self, path):
        return _latest_turn_offset(path)

    def _resolve(self, info, unique_workspace=False):
        # An exact live Herdr identity supersedes a saved manual association.
        # Validate it before changing any binding; never choose a newer cwd match.
        if session_of(info):
            return validate_rollout(resolve_session(info, home=self.home))
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

    def row_events(self, source_key, raw, cursor):
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
        return [(_stable_id(source_key, raw, cursor, event["role"], event["text"]), event)] if event else []
