"""Incremental, read-only Codex rollout reader for pane-channel conversation sync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .codex_session import (AUTHORITY_ERROR, LIVE_PROOF, NATIVE_STATUS, TITLE_SOURCE, resolve_session,
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
        db.execute("CREATE TABLE IF NOT EXISTS codex_input_readiness (source_key TEXT PRIMARY KEY, record TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS codex_completion_pending
            (event_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, turn_id TEXT NOT NULL, record TEXT NOT NULL)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(codex_completion_pending)")}
        for column in ("validated", "conflicted"):
            if column not in columns:
                default = 1 if column == "validated" else 0
                db.execute(f"ALTER TABLE codex_completion_pending ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}")
        db.execute("""CREATE TABLE IF NOT EXISTS codex_completion_turns
            (source_key TEXT NOT NULL, turn_id TEXT NOT NULL, event_id TEXT NOT NULL,
             PRIMARY KEY(source_key,turn_id))""")
        # Modern closed ledger entries already prove their exact source/turn.
        # Seed that certificate on upgrade without creating delivery events.
        for (raw,) in db.execute("SELECT record FROM codex_completions ORDER BY rowid"):
            record = json.loads(raw)
            if record.get("turn_id"):
                db.execute("INSERT OR IGNORE INTO codex_completion_turns VALUES (?,?,?)",
                           (record["source_key"], record["turn_id"], record["event_id"]))

    def input_blocker(self, info, previous=None, *, states=None, deltas=None):
        """Read-only FIFO evidence, independent of direct-input/steering capacity."""
        native = info.get(NATIVE_STATUS)
        if native:
            if native.get("thread_id") != session_of(info):
                raise SessionUnavailable("Codex readiness identity changed")
            if native.get("status") == "active":
                return "codex_native_busy"
            if native.get("status") != "idle":
                return "codex_native_readiness_unavailable"
        proof = info.get(LIVE_PROOF)
        if proof:
            path = self._fresh_receipt_path(info)
            try:
                path.stat()
            except FileNotFoundError:
                # A prior write cannot use the missing first-rollout case.
                return None if native and native.get("status") == "idle" and previous is None else "codex_native_readiness_unavailable"
            validate_rollout((session_of(info), path, info["cwd"]))
        else:
            _, path, _ = self._resolve(info)
        if native and (native.get("path") != str(path) or native.get("cwd") != info.get("cwd")
                       or native.get("home") != str(self.home)):
            raise SessionUnavailable("Codex readiness source changed")
        owner = self._input_lifecycle(info, path, allow_orphan_close=bool(native), states=states, deltas=deltas)
        if not owner["complete"] or not owner["known"]:
            return "codex_native_readiness_unavailable"
        if owner["active"] is not None:
            return "codex_native_busy"
        if previous:
            barrier = self._input_lifecycle(info, path, previous, states=states, deltas=deltas)
            if not barrier["complete"] or not barrier["closed"] or barrier["active"] is not None:
                return "codex_native_turn_unsettled"
        return None

    def _input_lifecycle(self, info, path, previous=None, limit=16 * 1024 * 1024, allow_orphan_close=False, *, states=None, deltas=None):
        """Persist bounded incremental lifecycle scans, never rescan old history.

        The general scan starts near EOF on first attach. A confirmed input's
        separate barrier starts at its committed pre-write cutoff and requires
        its exact native receipt and a matching closure of that same turn.
        """
        stat = path.stat()
        terminal = info.get("terminal_id") or info.get("pane_id", "")
        key = f"{terminal}:{session_of(info)}:" + (f"input:{previous['seq']}" if previous else "owner")
        expected = None
        start = max(0, stat.st_size - limit)
        if previous:
            baseline = previous.get("receipt_baseline", {})
            start = baseline.get("offset")
            same_file = (baseline.get("missing") is True and start == 0 or
                         baseline.get("device") == stat.st_dev and baseline.get("inode") == stat.st_ino)
            if (baseline.get("unavailable") or not same_file or type(start) is not int
                    or not 0 <= start <= stat.st_size or baseline.get("path") != str(path)
                    or baseline.get("source_key") != f"{terminal}:{session_of(info)}"):
                raise SessionUnavailable("Codex prior input source changed")
            expected = previous["prompt"]
        row = states.get(key) if states is not None else self.db.execute("SELECT record FROM codex_input_readiness WHERE source_key=?", (key,)).fetchone()
        state = json.loads(row[0]) if row else None
        if state and state.get("anchor") and stat.st_size >= state["offset"]:
            with path.open("rb") as stream:
                stream.seek(max(0, state["offset"] - 256))
                anchor = hashlib.sha256(stream.read(min(256, state["offset"]))).hexdigest()
            if anchor != state["anchor"]:
                if previous:
                    raise SessionUnavailable("Codex prior input lifecycle was rewritten")
                state = None
        if not state or (state["path"], state["device"], state["inode"]) != (str(path), stat.st_dev, stat.st_ino) or stat.st_size < state["offset"]:
            state = {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
                     "offset": start, "known": start == 0 or previous is not None,
                     "active": None, "receipt_turn": None, "closed": False}
            if not previous and start:
                with path.open("rb") as stream:
                    stream.seek(start)
                    fragment = stream.readline(limit + 1)
                    if not fragment.endswith(b"\n"):
                        raise SessionUnavailable("Codex lifecycle tail has no complete records")
                    state["offset"] = stream.tell()
        end = min(stat.st_size, state["offset"] + limit)
        with path.open("rb") as stream:
            stream.seek(state["offset"])
            while stream.tell() < end:
                # Native tool outputs can exceed 1 MiB. Read a complete row
                # within this scan's budget; never skip an oversized/partial
                # row that could contain lifecycle evidence.
                line = stream.readline(end - stream.tell() + 1)
                if not line.endswith(b"\n") or stream.tell() > end:
                    break
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise SessionUnavailable("Codex lifecycle record is invalid")
                payload = raw.get("payload", {})
                if raw.get("type") == "event_msg" and isinstance(payload, dict):
                    event, tid = payload.get("type"), payload.get("turn_id")
                    if event in ("task_started", "turn_started"):
                        if not isinstance(tid, str) or not tid:
                            raise SessionUnavailable("Codex turn start has no exact identity")
                        state["active"], state["known"] = tid, True
                    elif (event in ("task_complete", "turn_complete", "turn_aborted")
                          and isinstance(tid, str) and tid
                          and payload.get("thread_id", session_of(info)) == session_of(info)):
                        if state["receipt_turn"] == tid:
                            state["closed"] = True
                        if state["active"] == tid:
                            state["active"] = None
                        elif not previous and allow_orphan_close and not state["known"]:
                            # A long turn's start can predate the initial tail.
                            # Exact closure plus observed native idle settles
                            # that tail; a later unmatched start still blocks.
                            state["known"] = True
                    elif (expected is not None
                          and expected in self.receipt_user_texts(raw, state["offset"])):
                        item = payload.get("item")
                        if (event == "item_completed" and isinstance(item, dict)
                                and item.get("type") == "UserMessage"
                                and payload.get("thread_id") == session_of(info)
                                and isinstance(tid, str) and tid):
                            # An upgrade can encounter an old mid-turn write:
                            # its start predates the committed receipt cutoff.
                            # Modern receipts independently name the owned turn.
                            state["receipt_turn"] = tid
                        elif (state["active"] and tid in (None, state["active"])
                              and payload.get("thread_id", session_of(info)) == session_of(info)):
                            state["receipt_turn"] = state["active"]
                state["offset"] = stream.tell()
            stream.seek(max(0, state["offset"] - 256))
            state["anchor"] = hashlib.sha256(stream.read(min(256, state["offset"]))).hexdigest()
        after = path.stat()
        if (stat.st_dev, stat.st_ino) != (after.st_dev, after.st_ino) or after.st_size < stat.st_size:
            raise SessionUnavailable("Codex lifecycle source changed while reading")
        if deltas is not None:
            deltas[key] = json.dumps(state)
        else:
            self.db.execute("INSERT OR REPLACE INTO codex_input_readiness VALUES (?,?)", (key, json.dumps(state)))
        return {**state, "complete": state["offset"] == after.st_size}

    def latest_turn_offset(self, path):
        return _latest_turn_offset(path)

    def receipt_baseline(self, info):
        proof = info.get(LIVE_PROOF)
        if not proof:
            return super().receipt_baseline(info)
        tid = session_of(info)
        path = self._fresh_receipt_path(info)
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

    def _fresh_receipt_path(self, info):
        proof = info.get(LIVE_PROOF)
        tid = session_of(info)
        if (info.get(AUTHORITY_ERROR) or not isinstance(proof, dict)
                or info.get("agent_session", {}).get("source") != TITLE_SOURCE
                or proof.get("thread_id") != tid or proof.get("cwd") != info.get("cwd")
                or proof.get("home") != str(self.home) or not isinstance(proof.get("route"), dict)
                or proof["route"].get("home") != str(self.home.resolve())):
            raise SessionUnavailable("Codex fresh receipt proof differs from current identity")
        return native_rollout_path(proof.get("path"), tid, self.home)

    def initial_observation(self, info, session, *, records=None):
        # Reserve already commits the exact receipt baseline in Herdr SQLite.
        # Recover that cutoff even if a very fast first turn completes before
        # discovery, or the bridge restarts. Never override an existing cursor.
        tid, path, cwd = session
        if records is None:
            records = []
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_inputs'").fetchone():
                terminal = info.get("terminal_id") or info.get("pane_id", "")
                records = self.db.execute("SELECT record FROM terminal_inputs WHERE terminal_id=? AND session_id=? "
                    "AND state IN ('reserved','observing','uncertain','confirmed') ORDER BY seq", (terminal, tid)).fetchall()
        terminal = info.get("terminal_id") or info.get("pane_id", "")
        if records:
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
        binding = info["_bridge_binding"] if "_bridge_binding" in info else self.binding_snapshot(info)
        if binding and binding[1] != info.get("cwd"):
            raise SessionUnavailable("Bound directory changed; bind the current session again")
        return validate_rollout(resolve_session(info, home=self.home,
            unique_workspace=unique_workspace, thread_id=binding[0] if binding else None))

    def binding_snapshot(self, info):
        return self.db.execute("SELECT thread_id,cwd FROM codex_session_bindings WHERE terminal_id=?", (info.get("terminal_id"),)).fetchone()

    def bound_info(self, info, *, binding=...):
        if binding is ...:
            binding = self.binding_snapshot(info)
        if not binding:
            return info
        tid, _, _ = self._resolve({**info, "_bridge_binding": binding})
        return {**info, "agent_session": {"value": tid}}

    def validate_binding(self, info, thread_id):
        if not info.get("terminal_id"):
            raise SessionUnavailable("Exact terminal id required for binding")
        return validate_rollout(resolve_session(info, home=self.home, thread_id=thread_id))

    def apply_binding(self, info, session):
        tid, _, cwd = session
        self.db.execute("INSERT OR REPLACE INTO codex_session_bindings VALUES (?,?,?)", (info["terminal_id"], tid, cwd))
        self._status(info, "ready", thread_id=tid, source="explicit", reason="Bound; waiting for next scan")
        return self.diagnostic(info)

    def bind(self, info, thread_id):
        return self.apply_binding(info, self.validate_binding(info, thread_id))

    def record_answer(self, record, *, new=True):
        # Preserve immutable modern registration. Replayed IDs can revalidate
        # that registration, but never synthesize a legacy missed completion.
        if not record.get("turn_id"):
            return
        if self.db.execute("SELECT 1 FROM codex_completion_turns WHERE source_key=? AND turn_id=?",
                           (record["source_key"], record["turn_id"])).fetchone():
            return  # Changed native item IDs cannot recertify a closed turn.
        old = self.db.execute("SELECT record FROM codex_completion_pending WHERE event_id=?",
                              (record["event_id"],)).fetchone()
        if old:
            original = json.loads(old[0])
            same = all(original.get(key) == record.get(key) for key in
                       ("source_key", "turn_id", "text", "phase"))
            if same:
                self.db.execute("UPDATE codex_completion_pending SET validated=1 "
                                "WHERE event_id=? AND conflicted=0", (record["event_id"],))
            else:
                self.db.execute("UPDATE codex_completion_pending SET validated=0,conflicted=1 WHERE event_id=?",
                                (record["event_id"],))
        elif new:
            self.db.execute("""INSERT INTO codex_completion_pending
                (event_id,source_key,turn_id,record) VALUES (?,?,?,?)""",
                (record["event_id"], record["source_key"], record["turn_id"], json.dumps(record)))

    def apply_reset(self, source_key):
        self.db.execute("UPDATE codex_completion_pending SET validated=0 WHERE source_key=?", (source_key,))

    def reset_source(self, source_key, path=None):
        turns = {row[0] for row in self.db.execute("SELECT turn_id FROM codex_completion_pending WHERE source_key=?", (source_key,))}
        if not turns:
            return None
        self.apply_reset(source_key)
        return self.read_reset_offset(path, turns)

    def read_reset_offset(self, path, turns):
        # Re-read only the bounded suffix needed by registered unresolved turns.
        # Ordinary first-attach history remains skipped, including legacy IDs.
        size = path.stat().st_size
        start = max(0, size - 16 * 1024 * 1024)
        with path.open("rb") as stream:
            stream.seek(start)
            if start:
                stream.readline()
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                try:
                    raw = json.loads(line)
                    payload = raw.get("payload", {})
                    if (raw.get("type") == "event_msg" and isinstance(payload, dict)
                            and payload.get("turn_id") in turns):
                        return stream.tell() - len(line)
                except (ValueError, AttributeError, TypeError):
                    continue
        return None  # Missing evidence holds the registration without replay.

    def _close_answer(self, source_key, turn, payload, cursor, timestamp):
        if self.db.execute("SELECT 1 FROM codex_completion_turns WHERE source_key=? AND turn_id=?",
                           (source_key, turn)).fetchone():
            self.db.execute("DELETE FROM codex_completion_pending WHERE source_key=? AND turn_id=?",
                            (source_key, turn))
            return
        rows = self.db.execute("SELECT record,validated,conflicted FROM codex_completion_pending "
            "WHERE source_key=? AND turn_id=? ORDER BY rowid", (source_key, turn)).fetchall()
        records = [json.loads(row[0]) for row in rows]
        if not records or any(row[2] for row in rows):
            return  # Conflicting immutable evidence cannot certify any answer.
        if "last_agent_message" in payload:
            canonical = payload["last_agent_message"]
            if not isinstance(canonical, str) or not canonical.strip():
                return
            eligible = [record for record, row in zip(records, rows)
                        if row[1] and record["text"] == canonical.strip()]
            explicit = [record for record in eligible if record.get("phase") == "final_answer"]
            # Native last_agent_message identifies the last matching final item.
            selected = explicit[-1] if explicit else eligible[-1] if eligible else None
        else:
            # Older closure records lack canonical text. Only one explicitly
            # final candidate is unambiguous; phase-absent/multiple items hold.
            selected = records[0] if (len(records) == 1 and rows[0][1]
                                      and records[0].get("phase") == "final_answer") else None
        if selected is None:
            return
        selected["closure"] = {"kind": payload["type"], "turn_id": turn, "offset": cursor,
                               "timestamp": timestamp}
        # Both this certificate and the immutable ledger insert live in the
        # transcript savepoint; neither can survive the other's failed write.
        inserted = self.db.execute("INSERT OR IGNORE INTO codex_completion_turns VALUES (?,?,?)",
                                   (source_key, turn, selected["event_id"])).rowcount
        if inserted:
            self.queue_completion(selected)
        self.db.execute("DELETE FROM codex_completion_pending WHERE source_key=? AND turn_id=?",
                        (source_key, turn))

    def row_events(self, source_key, raw, cursor, *, info=None, agent=None):
        payload = raw.get("payload", {})
        event = None
        if raw.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("thread_id", source_key.rsplit(":", 1)[-1]) != source_key.rsplit(":", 1)[-1]:
                raise SessionUnavailable("Codex transcript changed session while reading")
            kind, turn = payload.get("type"), payload.get("turn_id")
            if kind in ("task_complete", "turn_complete", "turn_aborted") and isinstance(turn, str) and turn:
                if kind == "turn_aborted":
                    self.db.execute("DELETE FROM codex_completion_pending WHERE source_key=? AND turn_id=?",
                                    (source_key, turn))
                else:
                    self._close_answer(source_key, turn, payload, cursor, raw.get("timestamp"))
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
                        phase = item.get("phase")
                        event = {"role": "assistant", "text": text, "_turn_id": payload.get("turn_id"),
                                 "_phase": "final_answer" if phase in ("final_answer", "finalAnswer") else phase}
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
