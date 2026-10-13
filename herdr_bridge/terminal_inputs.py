"""Durable, one-shot prompt records and conservative composer/receipt guards."""
from __future__ import annotations

import json
import re


STARTED_STATES = ("reserved", "observing", "uncertain")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _visible(value: str) -> str:
    return ANSI.sub("", value).replace("\xa0", " ")


def _dim_suffix(raw: str, marker: str) -> bool:
    """True when every visible non-space after marker is terminal dim text."""
    position = raw.find(marker)
    if position < 0:
        return False
    dim = False
    saw = False
    cursor = position + len(marker)
    while cursor < len(raw):
        match = ANSI.match(raw, cursor)
        if match:
            final = match.group()[-1]
            if final == "m":
                try:
                    params = [int(value or 0) for value in match.group()[2:-1].split(";")]
                except ValueError:
                    params = []
                offset = 0
                while offset < len(params):
                    value = params[offset]
                    if value == 0:
                        dim = False
                    elif value == 2:
                        dim = True
                    elif value == 22:
                        dim = False
                    # In 38/48/58;2;r;g;b, the 2 selects truecolor and is not
                    # the SGR dim attribute. Likewise skip indexed colors.
                    if value in (38, 48, 58) and offset + 1 < len(params):
                        offset += 4 if params[offset + 1] == 2 else 2 if params[offset + 1] == 5 else 0
                    offset += 1
            cursor = match.end()
            continue
        char = raw[cursor]
        if not char.isspace():
            if not dim:
                return False
            saw = True
        cursor += 1
    return saw


def known_empty_composer(kind: str, screen: str) -> bool:
    """Accept only the observed bottom-composer layouts, never transcript history."""
    if not isinstance(screen, str):
        return False
    raw_lines = screen.splitlines()
    lines = [_visible(line).rstrip() for line in raw_lines]
    if kind == "codex":
        prompts = [(index, line.strip()) for index, line in enumerate(lines)
                   if line.lstrip().startswith("›")]
        if not prompts or prompts[-1][1] not in ("›", "› Ask Codex to do anything"):
            return False
        prompt_index, prompt = prompts[-1]
        if prompt != "›" and not _dim_suffix(raw_lines[prompt_index], "›"):
            return False
        footer = [line.strip() for line in lines[prompt_index + 1:] if line.strip()]
        # The startup banner scrolls away. The empty composer remains the final
        # prompt followed by exactly Codex's two bottom status rows. Requiring
        # the final prompt rejects a historical empty composer above a draft.
        return (len(footer) == 2 and " · " in footer[0]
                and "for agents" in footer[1] and "for short" in footer[1])
    if kind == "claude":
        nonempty = [(i, line.strip()) for i, line in enumerate(lines) if line.strip()]
        prompts = [(i, value) for i, value in nonempty if value.startswith("❯")]
        if not prompts:
            return False
        index, visible_prompt = prompts[-1]
        raw_prompt = raw_lines[index]
        if visible_prompt != "❯" and not _dim_suffix(raw_prompt, "❯"):
            return False
        before = next((value for i, value in reversed(nonempty) if i < index), "")
        after = next((value for i, value in nonempty if i > index), "")
        rule = lambda value: len(value) >= 8 and set(value) <= {"─"}
        after_index = next((position for position, value in nonempty if position > index and rule(value)), None)
        tail = [value for position, value in nonempty if after_index is not None and position > after_index]
        return (rule(before) and rule(after) and after_index is not None
                and len(tail) <= 3 and all("❯" not in value and not rule(value) for value in tail))
    return False


class TerminalInputs:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        db.execute("""CREATE TABLE IF NOT EXISTS terminal_inputs
            (seq INTEGER PRIMARY KEY AUTOINCREMENT,
             source TEXT NOT NULL, account_id TEXT NOT NULL,
             channel_id TEXT NOT NULL, message_ts TEXT NOT NULL,
             terminal_id TEXT NOT NULL, session_id TEXT NOT NULL,
             state TEXT NOT NULL, record TEXT NOT NULL,
             UNIQUE(source, account_id, channel_id, message_ts))""")
        rows = db.execute("SELECT seq,record,state FROM terminal_inputs WHERE state IN ('reserved','observing')").fetchall()
        for seq, raw, state in rows:
            rec = json.loads(raw)
            rec.update(state="uncertain", reason="bridge_restarted_after_reservation",
                       recovered_at=clock(), prior_state=state)
            db.execute("UPDATE terminal_inputs SET state='uncertain',record=? WHERE seq=?",
                       (json.dumps(rec, ensure_ascii=False), seq))

    def find(self, source, account_id, channel_id, message_ts):
        row = self.db.execute("""SELECT seq,state,record FROM terminal_inputs
            WHERE source=? AND account_id=? AND channel_id=? AND message_ts=?""",
            (source, account_id, channel_id, message_ts)).fetchone()
        if not row:
            return None
        rec = json.loads(row[2])
        return {**rec, "seq": row[0], "state": row[1]}

    def insert(self, record, state="queued"):
        record = {**record, "state": state, "created_at": self.clock()}
        cursor = self.db.execute("""INSERT INTO terminal_inputs
            (source,account_id,channel_id,message_ts,terminal_id,session_id,state,record)
            VALUES (?,?,?,?,?,?,?,?)""",
            (record["source"], record["account_id"], record["channel_id"],
             record["message_ts"], record["terminal_id"], record["session_id"],
             state, json.dumps(record, ensure_ascii=False)))
        record["seq"] = cursor.lastrowid
        return record

    def get(self, seq):
        row = self.db.execute("SELECT state,record FROM terminal_inputs WHERE seq=?", (seq,)).fetchone()
        if not row:
            return None
        return {**json.loads(row[1]), "seq": seq, "state": row[0]}

    def save(self, record, state, **fields):
        record = {**record, **fields, "state": state, "updated_at": self.clock()}
        self.db.execute("UPDATE terminal_inputs SET state=?,record=? WHERE seq=?",
                        (state, json.dumps(record, ensure_ascii=False), record["seq"]))
        return record

    def queued(self):
        return [self.get(row[0]) for row in self.db.execute(
            "SELECT seq FROM terminal_inputs WHERE state='queued' ORDER BY seq")]

    def started(self):
        return [self.get(row[0]) for row in self.db.execute(
            "SELECT seq FROM terminal_inputs WHERE state IN ('reserved','observing','uncertain') ORDER BY seq")]

    def blocker(self, record):
        """Return an unresolved write on this terminal, including after restart."""
        for started in self.started():
            if (started["seq"] != record.get("seq")
                    and started.get("terminal_id") == record.get("terminal_id")
                    and not started.get("arbiter_released")):
                return started
        return None

    def is_first(self, record):
        row = self.db.execute("""SELECT seq FROM terminal_inputs
            WHERE state='queued' AND terminal_id=? AND session_id=? ORDER BY seq LIMIT 1""",
            (record["terminal_id"], record["session_id"])).fetchone()
        return bool(row and row[0] == record["seq"])

    def last_dispatched(self, record):
        """A recorded receipt releases the write arbiter, not the native turn."""
        row = self.db.execute("""SELECT seq FROM terminal_inputs
            WHERE terminal_id=? AND session_id=? AND seq<?
            AND state IN ('reserved','observing','uncertain','confirmed')
            ORDER BY seq DESC LIMIT 1""", (record["terminal_id"], record["session_id"], record["seq"])).fetchone()
        return self.get(row[0]) if row else None

    @staticmethod
    def result(record):
        result = {"input_id": record["seq"], "pane_id": record.get("pane_id"),
                  "kind": record.get("agent_kind"), "outcome": record["state"]}
        if record.get("reason"):
            result["reason"] = record["reason"]
        if record.get("thread_id"):
            result["thread_id"] = record["thread_id"]
        return result
