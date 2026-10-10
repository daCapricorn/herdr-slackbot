"""Domain events and exact, one-shot terminal replies. No channel SDK imports."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .dialog import keys_for, parse_dialog
from .events import session_of


class BridgeError(ValueError):
    pass


def identity(info):
    # A session alone is not enough: terminal incarnations must also match.
    return (session_of(info), info.get("terminal_id"), info.get("agent"))


def public_agent(info):
    return {"pane_id": info["pane_id"], "session": session_of(info),
            "terminal_id": info.get("terminal_id"), "kind": info.get("agent"),
            "status": info.get("agent_status"), "name": info.get("name"),
            "cwd": info.get("cwd"), "title": info.get("terminal_title_stripped"),
            "workspace_id": info.get("workspace_id")}


class Bridge:
    def __init__(self, client, state_path, *, clock=time.time, approval_ttl=900):
        self.client, self.clock, self.approval_ttl = client, clock, approval_ttl
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_path, check_same_thread=False)
        if os.name != "nt":
            os.chmod(state_path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS approvals
              (id TEXT PRIMARY KEY, record TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox
              (id TEXT PRIMARY KEY, event TEXT NOT NULL);
        """)
        self.lock = threading.RLock()

    def status(self):
        return [public_agent(a) for a in self.client.list_agents()]

    def _agent(self, pane):
        if not isinstance(pane, str) or ":p" not in pane:
            raise BridgeError("Use an exact pane ID, for example w4:p2")
        info = self.client.get_agent(pane)
        if info.get("pane_id") != pane:
            raise BridgeError("Pane identity mismatch")
        if not info.get("terminal_id"):
            raise BridgeError("Herdr did not provide a terminal identity")
        return info

    def read(self, pane, lines=120):
        info = self._agent(pane)
        # Visible reads do not scroll or disturb a blocked/working TUI.
        text = self.client.read_agent_once(pane, max(1, min(int(lines), 300)), source="visible")
        if identity(self._agent(pane)) != identity(info):
            raise BridgeError("Agent changed during read")
        return {"agent": public_agent(info), "text": text}

    def _queue(self, kind, agent, **fields):
        event = {"id": uuid.uuid4().hex, "type": kind, "at": self.clock(),
                 "agent": public_agent(agent), **fields}
        self.db.execute("INSERT INTO outbox VALUES (?, ?)", (event["id"], json.dumps(event)))
        return event

    def discover(self, info):
        """Recover already-blocked agents; repeat scans also catch a redrawn dialog."""
        if info.get("agent_status") != "blocked":
            return
        pane = info["pane_id"]
        screen = self.client.read_agent_once(pane, 160, source="visible")
        fresh = self._agent(pane)
        if identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked":
            return
        kind = info.get("agent")
        dialog = parse_dialog(screen, kind) if kind in ("codex", "claude") else None
        with self.lock, self.db:
            rows = self.db.execute("SELECT id, record, state FROM approvals").fetchall()
            for request_id, raw, state in rows:
                rec = json.loads(raw)
                if (tuple(rec["identity"]) == identity(info) and rec["pane_id"] == pane
                        and rec.get("sequence") == info.get("state_change_seq")
                        and rec["fingerprint"] == (dialog.fingerprint if dialog else screen)
                        and (state != "pending" or rec["expires_at"] > self.clock())):
                    # Consumed/uncertain actions are never rearmed by polling.
                    return
            request_id = uuid.uuid4().hex
            options = []
            if dialog and not dialog.multi_select and not dialog.plan_file:
                options = [{"index": i, "label": o.label} for i, o in enumerate(dialog.options)
                           if not o.free_text and not o.chat]
            rec = {"id": request_id, "pane_id": pane, "identity": identity(info),
                   "sequence": info.get("state_change_seq"),
                   "fingerprint": dialog.fingerprint if dialog else screen,
                   "created_at": self.clock(), "expires_at": self.clock() + self.approval_ttl,
                   "options": options}
            self.db.execute("INSERT INTO approvals VALUES (?, ?, 'pending')", (request_id, json.dumps(rec)))
            self._queue("approval.requested" if options else "agent.blocked", info,
                        request=rec, text=screen)

    def transition(self, transition):
        if transition.ended or not transition.info:
            return
        info = transition.info
        if transition.status == "blocked":
            self.discover(info)
        elif transition.status in ("done", "idle") and transition.prev_status in ("working", "blocked"):
            text = self.client.read_agent_once(info["pane_id"], 160, source="visible")
            if identity(self._agent(info["pane_id"])) != identity(info):
                return
            with self.lock, self.db:
                self._queue("agent.done", info, text=text)

    def pending(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT record FROM approvals WHERE state='pending'")
                    if json.loads(r[0])["expires_at"] > self.clock()]

    def events(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT event FROM outbox ORDER BY rowid LIMIT 20")]

    def ack(self, event_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
        return {"acknowledged": event_id}

    def answer(self, request_id, option):
        with self.lock:
            row = self.db.execute("SELECT record, state FROM approvals WHERE id=?", (request_id,)).fetchone()
            if not row or row[1] != "pending":
                raise BridgeError("Request is absent or already answered; nothing was sent")
            rec = json.loads(row[0])
            if self.clock() >= rec["expires_at"]:
                raise BridgeError("Request expired; nothing was sent")
            if type(option) is not int or option not in {o["index"] for o in rec["options"]}:
                raise BridgeError("Unsupported option; answer this dialog locally")
            info = self._agent(rec["pane_id"])
            if (tuple(rec["identity"]) != identity(info) or info.get("agent_status") != "blocked"
                    or rec.get("sequence") != info.get("state_change_seq")):
                raise BridgeError("Agent changed or is no longer blocked; nothing was sent")
            screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
            dialog = parse_dialog(screen, info.get("agent"))
            if not dialog or dialog.fingerprint != rec["fingerprint"]:
                raise BridgeError("Dialog changed; nothing was sent")
            fresh = self._agent(rec["pane_id"])
            if identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked":
                raise BridgeError("Agent changed before dispatch; nothing was sent")
            # Commit BEFORE touching the terminal. A lost reply/crash must never replay keys.
            with self.db:
                self.db.execute("UPDATE approvals SET state='dispatched' WHERE id=?", (request_id,))
            try:
                self.client.send_keys(rec["pane_id"], keys_for(dialog, option))
            except Exception:
                with self.db:
                    self.db.execute("UPDATE approvals SET state='uncertain' WHERE id=?", (request_id,))
                raise
            return {"request_id": request_id, "outcome": "dispatched", "option": dialog.options[option].label}

    def send(self, pane, text):
        with self.lock:
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise BridgeError("Prompt must contain 1–16000 characters")
            info = self._agent(pane)
            if info.get("agent_status") not in ("idle", "done"):
                raise BridgeError("Agent must be idle/done; use an exact approval request to answer a dialog")
            fresh = self._agent(pane)
            if identity(fresh) != identity(info) or fresh.get("agent_status") not in ("idle", "done"):
                raise BridgeError("Agent changed before dispatch")
            self.client.prompt_agent(pane, text)
            return {"pane_id": pane, "outcome": "dispatched"}

    def close(self):
        self.db.close()
