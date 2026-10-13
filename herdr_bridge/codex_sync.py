"""Incremental, read-only Codex rollout reader for pane-channel conversation sync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .codex_session import (AUTHORITY_ERROR, LIVE_PROOF, TITLE_SOURCE, resolve_session,
                            native_rollout_path, validate_rollout, SessionUnavailable)
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
    value = r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;"']+)'''

    def credential(match):
        secret = match[2]
        replacement = secret[0] + "[redacted]" + secret[-1] if secret[0] in "\"'" else "[redacted]"
        return match[1] + replacement

    text = re.sub(r"(?i)(\b[A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API[_-]?KEY)\b\s*[=:]\s*)" + value, credential, text)
    text = re.sub(r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)" + value, credential, text)
    text = re.sub(r"(?i)(--(?:token|password|secret|api-key)(?:=|\s+))" + value, credential, text)
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

    def receipt_baseline(self, info):
        proof = info.get(LIVE_PROOF)
        if not proof:
            return super().receipt_baseline(info)
        tid = session_of(info)
        if (info.get(AUTHORITY_ERROR) or not isinstance(proof, dict)
                or info.get("agent_session", {}).get("source") != TITLE_SOURCE
                or proof.get("thread_id") != tid or proof.get("cwd") != info.get("cwd")
                or proof.get("home") != str(self.home) or not isinstance(proof.get("route"), dict)
                or proof["route"].get("home") != str(self.home.resolve())):
            raise SessionUnavailable("Codex fresh receipt proof differs from current identity")
        path = native_rollout_path(proof.get("path"), tid, self.home)
        baseline = {"source_key": f"{info.get('terminal_id') or info.get('pane_id', '')}:{tid}",
                    "path": str(path), "offset": 0}
        try:
            stat = path.stat()
        except FileNotFoundError:
            try:
                stat = path.stat()  # Capture a rollout materialized during the first stat.
            except FileNotFoundError:
                return {**baseline, "missing": True, "native_identity": proof}
        # Existing files, including files appearing after metadata read, must
        # pass the usual session_meta contract before snapshotting their size.
        validate_rollout((tid, path, info["cwd"]))
        after = path.stat()
        if (stat.st_dev, stat.st_ino) != (after.st_dev, after.st_ino) or after.st_size < stat.st_size:
            raise SessionUnavailable("Codex rollout changed while capturing receipt baseline")
        return {**baseline, "device": after.st_dev, "inode": after.st_ino, "offset": after.st_size,
                "native_identity": proof}

    def initial_observation(self, info, session):
        # Reserve already commits the exact receipt baseline in Herdr SQLite.
        # Recover that cutoff even if a very fast first turn completes before
        # discovery, or the bridge restarts. Never override an existing cursor.
        tid, path, cwd = session
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_inputs'").fetchone():
            terminal = info.get("terminal_id") or info.get("pane_id", "")
            records = self.db.execute("SELECT record FROM terminal_inputs WHERE terminal_id=? AND session_id=? "
                "AND state IN ('reserved','observing','uncertain','confirmed') ORDER BY seq", (terminal, tid))
            for (raw,) in records:
                rec = json.loads(raw)
                baseline = rec.get("receipt_baseline", {})
                proof = baseline.get("native_identity", {})
                if (rec.get("agent_kind") == "codex" and rec.get("attempt_id")
                        and baseline.get("source_key") == f"{terminal}:{tid}" and baseline.get("path") == str(path)
                        and proof.get("thread_id") == tid and proof.get("cwd") == cwd
                        and proof.get("home") == str(self.home) and proof.get("path") == str(path)):
                    native_rollout_path(str(path), tid, self.home)
                    stat = path.stat()
                    offset = baseline.get("offset")
                    same_file = (baseline.get("missing") is True and offset == 0 or
                                 baseline.get("device") == stat.st_dev and baseline.get("inode") == stat.st_ino)
                    if same_file and type(offset) is int and 0 <= offset <= stat.st_size:
                        return {"offset": offset}
        return super().initial_observation(info, session)

    def _resolve(self, info, unique_workspace=False):
        if info.get(AUTHORITY_ERROR):
            raise SessionUnavailable(info[AUTHORITY_ERROR])
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

    def receipt_user_texts(self, raw, offset):
        payload = raw.get("payload", {})
        if raw.get("type") != "event_msg" or not isinstance(payload, dict):
            return ()
        if payload.get("type") == "user_message":
            text = _clean_user_text(str(payload.get("message") or ""))
        elif payload.get("type") == "item_completed" and isinstance(payload.get("item"), dict):
            text = _user_item(payload["item"])
        else:
            text = None
        return (text,) if text else ()
