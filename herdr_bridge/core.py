"""Domain events and exact, one-shot terminal replies. No channel SDK imports."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .dialog import KIND_UNKNOWN, keys_for, parse_dialog
from .events import session_of
from .metadata import session_model

log = logging.getLogger(__name__)
CONSUMED_STATES = {"dispatching", "dispatched", "confirmed", "unconfirmed", "uncertain"}


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


def current_dialog(screen, kind):
    # Codex does not draw a top rule. Its transcript above the approval keeps
    # changing (tool output, elapsed timers); that is not approval evidence.
    if kind == "codex":
        lines = screen.splitlines()
        anchors = [i for i, line in enumerate(lines)
                   if line.strip() == "Would you like to run the following command?"]
        if not anchors:
            return None
        screen = "\n".join(lines[anchors[-1]:])
    dialog = parse_dialog(screen, kind) if kind in ("codex", "claude") else None
    return dialog if dialog and dialog.kind != KIND_UNKNOWN else None


class Bridge:
    def __init__(self, client, state_path, *, clock=time.time, approval_ttl=900,
                 monotonic=time.monotonic, sleep=time.sleep,
                 confirmation_timeout=5.0, confirmation_interval=0.2):
        self.client, self.clock, self.approval_ttl = client, clock, approval_ttl
        self.monotonic, self.sleep = monotonic, sleep
        self.confirmation_timeout = confirmation_timeout
        self.confirmation_interval = confirmation_interval
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
        agents = self.client.list_agents()
        return [self._public_agent(a, agents) for a in agents]

    def _public_agent(self, info, agents=None):
        result = public_agent(info)
        try:
            tabs = self.client.list_tabs(info.get("workspace_id"))
            tab = next((t for t in tabs if t.get("tab_id") == info.get("tab_id")), {})
            result["name"] = info.get("name") or tab.get("label")
        except Exception:
            pass  # Display metadata must never block terminal event handling.
        try:
            agents = self.client.list_agents() if agents is None else agents
            unique = sum(a.get("agent") == info.get("agent") and a.get("cwd") == info.get("cwd") for a in agents) == 1
            result["model"] = session_model(info, unique_workspace=unique)
        except Exception:
            result["model"] = None
        return result

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
        return {"agent": self._public_agent(info), "text": text}

    def _queue(self, kind, agent, **fields):
        event = {"id": uuid.uuid4().hex, "type": kind, "at": self.clock(),
                 "agent": self._public_agent(agent), **fields}
        self.db.execute("INSERT INTO outbox VALUES (?, ?)", (event["id"], json.dumps(event)))
        return event

    def discover(self, info):
        """Recover already-blocked agents; repeat scans also catch a redrawn dialog."""
        if info.get("agent_status") != "blocked":
            return
        pane = info["pane_id"]
        screen = self.client.read_agent_once(pane, 160, source="visible")
        fresh = self._agent(pane)
        if (identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked"
                or fresh.get("state_change_seq") != info.get("state_change_seq")):
            return
        kind = info.get("agent")
        dialog = current_dialog(screen, kind)
        fingerprint = dialog.fingerprint if dialog else None
        with self.lock, self.db:
            rows = self.db.execute("SELECT id, record, state FROM approvals").fetchall()
            for request_id, raw, state in rows:
                rec = json.loads(raw)
                if rec["pane_id"] != pane:
                    continue
                same_generation = (tuple(rec["identity"]) == identity(info)
                                   and rec.get("sequence") == info.get("state_change_seq"))
                if same_generation and state in CONSUMED_STATES:
                    return  # Never rearm after terminal delivery, even on redraw.
                if (same_generation and rec.get("evidence_version") == 1
                        and rec["fingerprint"] == fingerprint and state != "superseded"):
                    return  # Unknown screens and expired requests also notify only once.
                if state == "pending":
                    # Replace stale cards; a callback to them can no longer dispatch.
                    self.db.execute("UPDATE approvals SET state='superseded' WHERE id=?", (request_id,))
            request_id = uuid.uuid4().hex
            options = []
            if dialog and not dialog.multi_select and not dialog.plan_file:
                options = [{"index": i, "label": o.label} for i, o in enumerate(dialog.options)
                           if not o.free_text and not o.chat]
            rec = {"id": request_id, "pane_id": pane, "identity": identity(info),
                   "sequence": info.get("state_change_seq"),
                   "fingerprint": fingerprint, "evidence_version": 1,
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
        with self.lock, self.db:
            live = {a["pane_id"]: a for a in self.client.list_agents()}
            for request_id, raw in self.db.execute("SELECT id, record FROM approvals WHERE state='pending'").fetchall():
                rec = json.loads(raw)
                info = live.get(rec["pane_id"])
                if (not info or identity(info) != tuple(rec["identity"])
                        or info.get("agent_status") != "blocked"
                        or info.get("state_change_seq") != rec.get("sequence")):
                    self.db.execute("UPDATE approvals SET state='superseded' WHERE id=?", (request_id,))
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT record FROM approvals WHERE state='pending'")
                    if json.loads(r[0])["expires_at"] > self.clock()]

    def events(self):
        self.pending()  # Retire cards whose pane has already advanced.
        with self.lock, self.db:
            for event_id, raw in self.db.execute("SELECT id, event FROM outbox").fetchall():
                event = json.loads(raw)
                request = event.get("request")
                if request:
                    state = self.db.execute("SELECT state FROM approvals WHERE id=?", (request["id"],)).fetchone()
                    if not state or state[0] != "pending" or request["expires_at"] <= self.clock():
                        self.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
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
            dialog = current_dialog(screen, info.get("agent"))
            if not dialog or dialog.fingerprint != rec["fingerprint"]:
                raise BridgeError("Dialog changed; nothing was sent")
            fresh = self._agent(rec["pane_id"])
            if (identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked"
                    or fresh.get("state_change_seq") != rec.get("sequence")):
                raise BridgeError("Agent changed before dispatch; nothing was sent")
            keys = keys_for(dialog, option)
            rec["dispatch"] = {"option": option, "keys": keys, "started_at": self.clock(),
                               "focused": fresh.get("focused"), "revision": fresh.get("revision")}
            # Consume BEFORE touching the terminal, separately from confirmed acceptance.
            # A lost reply/crash must never replay keys.
            with self.db:
                self.db.execute("UPDATE approvals SET record=?, state='dispatching' WHERE id=?",
                                (json.dumps(rec), request_id))
            try:
                self.client.send_keys(rec["pane_id"], keys)
            except Exception:
                with self.db:
                    self.db.execute("UPDATE approvals SET state='uncertain' WHERE id=?", (request_id,))
                raise
            rec["dispatch"]["acknowledged_at"] = self.clock()
            verification = self._confirm_answer(rec)
            rec["verification"] = verification
            outcome = verification["outcome"]
            with self.db:
                self.db.execute("UPDATE approvals SET record=?, state=? WHERE id=?",
                                (json.dumps(rec), outcome, request_id))
            log.info("Herdr answer verification: %s option=%s outcome=%s reason=%s elapsed_ms=%s observation=%s",
                     request_id, option, outcome, verification["reason"], verification["elapsed_ms"],
                     verification.get("observation"))
            return {"request_id": request_id, "outcome": outcome,
                    "option": dialog.options[option].label, "verification": verification}

    def _confirm_answer(self, rec):
        """Observe only: a provider receipt is not proof the TUI accepted a key.

        Require two consistent observations that the original approval is gone.
        Never send more keys, even if the original dialog remains unchanged.
        """
        start = self.monotonic()
        deadline = start + self.confirmation_timeout
        candidate = None
        observation = None
        reason = "not_observed"
        while True:
            try:
                info = self._agent(rec["pane_id"])
                if identity(info) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
                screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
                fresh = self._agent(rec["pane_id"])
                if identity(fresh) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
                live = current_dialog(screen, fresh.get("agent"))
                seq, status = fresh.get("state_change_seq"), fresh.get("agent_status")
                same_dialog = live is not None and live.fingerprint == rec["fingerprint"]
                observation = {"status": status, "sequence": seq, "same_dialog": same_dialog}
                stable = (seq == info.get("state_change_seq") and status == info.get("agent_status"))
                advanced = (rec.get("sequence") is not None and seq is not None and seq != rec["sequence"])
                current = None
                if stable and not same_dialog:
                    if advanced and status in ("working", "idle", "done"):
                        current = ("approval_closed", seq, status)
                    elif status == "blocked" and live is not None:
                        current = ("next_dialog", seq, live.fingerprint)
                if current is not None and current == candidate:
                    return {"outcome": "confirmed", "reason": current[0],
                            "elapsed_ms": round((self.monotonic() - start) * 1000),
                            "observation": observation}
                candidate = current
                reason = "dialog_still_open" if same_dialog else "not_observed"
            except Exception:
                # Reads can fail after a successful write; do not turn that into another write.
                candidate = None
                observation = None
                reason = "read_failed"
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(self.confirmation_interval, remaining))
        return {"outcome": "unconfirmed" if reason == "dialog_still_open" else "uncertain",
                "reason": reason, "elapsed_ms": round((self.monotonic() - start) * 1000),
                "observation": observation}

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
