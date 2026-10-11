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
from .questions import CodexQuestionSource, QuestionUnavailable, fingerprint as question_fingerprint, input_plan

log = logging.getLogger(__name__)
CONSUMED_STATES = {"reserved", "dispatching", "dispatched", "confirmed", "unconfirmed", "uncertain"}


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
                 confirmation_timeout=5.0, confirmation_interval=0.2, followup_timeout=120,
                 question_source=None):
        self.client, self.clock, self.approval_ttl = client, clock, approval_ttl
        self.monotonic, self.sleep = monotonic, sleep
        self.confirmation_timeout = confirmation_timeout
        self.confirmation_interval = confirmation_interval
        self.followup_timeout = followup_timeout
        self.question_source = question_source or CodexQuestionSource()
        self.active = set()
        self.sending = {}  # pane -> request, only terminal writes/initial confirmation
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
            CREATE TABLE IF NOT EXISTS cards
              (request_id TEXT NOT NULL, binding TEXT NOT NULL,
               PRIMARY KEY(request_id, binding));
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
        questions = self.discover_questions(info)
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
        if questions and dialog is None:
            for question in questions.values():
                try:
                    input_plan(screen, question, 0 if question["options"] else None,
                               question["options"][0] if question["options"] else "", list(questions))
                except QuestionUnavailable:
                    continue
                return  # A verified expanded/collapsed async input already has its own card.
        fingerprint = dialog.fingerprint if dialog else None
        with self.lock, self.db:
            rows = self.db.execute("SELECT id, record, state FROM approvals").fetchall()
            for request_id, raw, state in rows:
                rec = json.loads(raw)
                if rec.get("kind") == "codex_question":
                    continue
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

    def _question_snapshot(self, info, agents=None):
        agents = self.client.list_agents() if agents is None else agents
        unique = sum(a.get("agent") == "codex" and a.get("cwd") == info.get("cwd") for a in agents) == 1
        return self.question_source.snapshot(info, unique_workspace=unique)

    def discover_questions(self, info):
        if info.get("agent") != "codex":
            return {}
        try:
            snapshot = self._question_snapshot(info)
        except QuestionUnavailable:
            return {}
        if not snapshot["pending"]:
            return {}
        screen = self.client.read_agent_once(info["pane_id"], 160, source="visible")
        fresh = self._agent(info["pane_id"])
        if identity(fresh) != identity(info):
            return {}
        with self.lock, self.db:
            for question in snapshot["pending"].values():
                fp = question_fingerprint(question)
                rows = self.db.execute("SELECT record,state FROM approvals").fetchall()
                if any(json.loads(raw).get("kind") == "codex_question"
                       and tuple(json.loads(raw)["identity"]) == identity(info)
                       and json.loads(raw)["pane_id"] == info["pane_id"]
                       and json.loads(raw)["fingerprint"] == fp and state != "superseded" for raw, state in rows):
                    continue
                rec = {"id": uuid.uuid4().hex, "kind": "codex_question", "question": question,
                       "pane_id": info["pane_id"], "identity": identity(info),
                       "sequence": info.get("state_change_seq"), "fingerprint": fp,
                       "created_at": self.clock(), "expires_at": self.clock() + self.approval_ttl,
                       "options": [{"index": i, "label": o} for i, o in enumerate(question["options"])]}
                self.db.execute("INSERT INTO approvals VALUES (?, ?, 'pending')", (rec["id"], json.dumps(rec)))
                self._queue("question.requested", info, request=rec, text=screen, summary=question["title"])
        return snapshot["pending"]

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
                if rec.get("kind") == "codex_question" and info and identity(info) == tuple(rec["identity"]):
                    try:
                        question = self._question_snapshot(info, list(live.values()))["pending"].get(rec["question"]["id"])
                    except QuestionUnavailable:
                        continue  # A failed read is not proof of expiry; dispatch still fails closed.
                    if question and question_fingerprint(question) == rec["fingerprint"]:
                        continue
                    self.db.execute("UPDATE approvals SET state='superseded' WHERE id=?", (request_id,))
                    continue
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

    def answer(self, request_id, option=None, answer_text=None):
        prepared = self.prepare_answer(request_id, option=option, answer_text=answer_text)
        if prepared.pop("ready_to_dispatch"):
            return self.dispatch_reserved(request_id, prepared["attempt_id"])
        if prepared["outcome"] in ("checking", "confirmed"):
            return prepared
        return self.check(request_id)

    def prepare_answer(self, request_id, option=None, retry_from=None, answer_text=None):
        """Reserve a human choice without writing, so OpenClaw can show progress first."""
        with self.lock:
            rec, state = self._record(request_id)
            if retry_from is not None:
                if retry_from in {a.get("attempt_id") for a in rec.get("attempts", [])}:
                    return {**self._result(rec, state), "ready_to_dispatch": False}
                if (state != "unconfirmed" or request_id in self.active
                        or retry_from != rec.get("dispatch", {}).get("attempt_id")
                        or not rec.get("verification", {}).get("retryable")):
                    # An unsubmitted reservation can be resumed only by its exact button.
                    if state == "reserved" and retry_from == rec["dispatch"]["attempt_id"]:
                        return {**self._result(rec, state), "outcome": "checking", "ready_to_dispatch": True}
                    raise BridgeError("Retry is stale or acceptance is unknown; check status first. Nothing was sent")
                option = rec["dispatch"]["option"]
                self._reserve(rec, option, rec["dispatch"].get("answer") if option is None else None)
            else:
                custom = rec.get("kind") == "codex_question" and answer_text is not None and option is None
                if custom:
                    if not isinstance(answer_text, str) or not answer_text.strip() or len(answer_text) > 8000:
                        raise BridgeError("Question answer must contain 1–8000 characters; nothing was sent")
                    answer_text = answer_text.strip()
                elif answer_text is not None or type(option) is not int or option not in {o["index"] for o in rec["options"]}:
                    raise BridgeError("Unsupported option; nothing was sent")
                if state in CONSUMED_STATES:
                    if (option != rec.get("dispatch", {}).get("option")
                            or custom and answer_text != rec["dispatch"].get("answer")):
                        raise BridgeError("A different option was already submitted; nothing was sent")
                    return {**self._result(rec, state),
                            "outcome": "checking" if state == "reserved" else self._result(rec, state)["outcome"],
                            "ready_to_dispatch": state == "reserved"}
                if state != "pending":
                    raise BridgeError("Request was superseded; nothing was sent")
                self._reserve(rec, option, answer_text)
            return {**self._result(rec, "reserved"), "outcome": "checking", "ready_to_dispatch": True}

    def dispatch_reserved(self, request_id, attempt_id):
        with self.lock:
            rec, state = self._record(request_id)
            if attempt_id != rec.get("dispatch", {}).get("attempt_id"):
                raise BridgeError("Submission is stale; nothing was sent")
            if state != "reserved":
                return self._result(rec, state)  # A duplicate commit never writes again.
            if rec.get("kind") == "codex_question":
                plan, fresh = self._live_question(rec, rec["dispatch"]["option"], rec["dispatch"].get("answer"))
                rec["dispatch"].update(plan)
            else:
                _, fresh = self._live_dialog(rec)  # The Slack edit may have taken seconds.
            if rec["pane_id"] in self.sending:
                raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent")
            rec["dispatch"].update(started_at=self.clock(), focused=fresh.get("focused"), revision=fresh.get("revision"))
            rec["followup_deadline"] = min(rec["expires_at"], self.clock() + self.followup_timeout)
            self._save(rec, "dispatching")
            self._claim_pane(rec)
            self.active.add(request_id)
        return self._finish_dispatch(rec)

    def _record(self, request_id):
        row = self.db.execute("SELECT record, state FROM approvals WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise BridgeError("Request is absent; nothing was sent")
        return json.loads(row[0]), row[1]

    def _save(self, rec, state):
        with self.db:
            self.db.execute("UPDATE approvals SET record=?, state=? WHERE id=?",
                            (json.dumps(rec), state, rec["id"]))

    def _live_dialog(self, rec):
        if self.clock() >= rec["expires_at"]:
            raise BridgeError("Request expired; nothing was sent")
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
        return dialog, fresh

    def _claim_pane(self, rec):
        if rec["pane_id"] in self.sending:
            raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent")
        self.sending[rec["pane_id"]] = rec["id"]

    def _live_question(self, rec, option, answer):
        if self.clock() >= rec["expires_at"]:
            raise BridgeError("Request expired; nothing was sent")
        info = self._agent(rec["pane_id"])
        if identity(info) != tuple(rec["identity"]):
            raise BridgeError("Agent changed; nothing was sent")
        try:
            snapshot = self._question_snapshot(info)
            question = snapshot["pending"].get(rec["question"]["id"])
            if not question or question_fingerprint(question) != rec["fingerprint"]:
                raise BridgeError("Question was answered, expired or changed; nothing was sent")
            screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
            plan = input_plan(screen, question, option, answer, list(snapshot["pending"]))
            fresh = self._agent(rec["pane_id"])
            if (identity(fresh) != identity(info) or fresh.get("state_change_seq") != info.get("state_change_seq")
                    or plan["mode"] == "prompt" and fresh.get("agent_status") not in ("working", "idle", "done", "blocked")):
                raise BridgeError("Question input changed or an approval is open; nothing was sent")
            if plan["mode"] == "prompt":
                # Herdr can classify the collapsed async summary as blocked even
                # though the main composer is visible and empty. agent.prompt
                # rejects all blocked input; use one encoded pane submission only
                # after the same exact question/composer checks, never as a retry.
                plan["transport"] = "pane" if fresh.get("agent_status") == "blocked" else "agent"
            # Recheck structured evidence after reading the terminal.
            current = self._question_snapshot(fresh)["pending"].get(question["id"])
            if not current or question_fingerprint(current) != rec["fingerprint"]:
                raise BridgeError("Question changed before dispatch; nothing was sent")
            return plan, fresh
        except QuestionUnavailable as exc:
            raise BridgeError(str(exc)) from exc

    def _reserve(self, rec, option, answer_text=None):
        if rec.get("kind") == "codex_question":
            answer = answer_text if option is None else rec["question"]["options"][option]
            plan, fresh = self._live_question(rec, option, answer)
        else:
            dialog, fresh = self._live_dialog(rec)
            plan = {"keys": keys_for(dialog, option)}
        if rec["pane_id"] in self.sending:
            raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent")
        if rec.get("dispatch"):
            rec.setdefault("attempts", []).append(rec["dispatch"])
        rec["dispatch"] = {"attempt_id": uuid.uuid4().hex, "option": option,
                           **plan, "started_at": self.clock(),
                           "focused": fresh.get("focused"), "revision": fresh.get("revision")}
        rec.pop("verification", None)
        rec.pop("watch_expired", None)
        rec["followup_deadline"] = min(rec["expires_at"], self.clock() + self.followup_timeout)
        self._save(rec, "reserved")

    def _finish_dispatch(self, rec):
        try:
            try:
                if rec["dispatch"].get("mode") == "prompt":
                    if rec["dispatch"].get("transport") == "pane":
                        self.client.submit_pane(rec["pane_id"], rec["dispatch"]["text"])
                    else:
                        self.client.prompt_agent(rec["pane_id"], rec["dispatch"]["text"])
                else:
                    self.client.send_keys(rec["pane_id"], rec["dispatch"]["keys"])
                rec["dispatch"]["acknowledged_at"] = self.clock()
            except Exception as exc:
                # An ambiguous receipt is still consumed; observe, never auto-retry.
                rec["dispatch"]["error_type"] = type(exc).__name__
            with self.lock:
                self._save(rec, "dispatching")
            verification = self._confirm_answer(rec)
            with self.lock:
                self.active.discard(rec["id"])
                if self.sending.get(rec["pane_id"]) == rec["id"]:
                    self.sending.pop(rec["pane_id"])
                return self._store_verification(rec, verification)
        finally:
            with self.lock:
                self.active.discard(rec["id"])
                if self.sending.get(rec["pane_id"]) == rec["id"]:
                    self.sending.pop(rec["pane_id"])

    def _result(self, rec, state):
        selected = rec.get("dispatch", {}).get("option")
        label = next((o["label"] for o in rec["options"] if o["index"] == selected), "Selected option")
        if rec.get("kind") == "codex_question":
            label = rec.get("dispatch", {}).get("answer", label)
        verification = rec.get("verification", {})
        retryable = (state in ("unconfirmed", "reserved") and verification.get("retryable") is True
                     and rec["id"] not in self.active and self.clock() < rec["expires_at"])
        return {"request_id": rec["id"], "kind": rec.get("kind", "approval"),
                "outcome": "checking" if rec["id"] in self.active else ("not_sent" if state == "reserved" else state),
                "option": label, "option_index": selected, "verification": verification,
                "attempt_id": rec.get("dispatch", {}).get("attempt_id"),
                "retry_token": rec.get("dispatch", {}).get("attempt_id") if retryable else None,
                "watching": (state != "confirmed" and self.clock() < rec.get("followup_deadline", 0)
                             and verification.get("reason") != "agent_replaced")}

    def _store_verification(self, rec, verification):
        rec["verification"] = verification
        outcome = "reserved" if verification["outcome"] == "not_sent" else verification["outcome"]
        result = self._result(rec, outcome)
        # The outbox carries only the latest result. Repeated edits are harmless.
        signature = [outcome, verification["reason"], result["retry_token"], result["watching"]]
        changed = signature != rec.get("last_result_signature")
        rec["last_result_signature"] = signature
        with self.db:
            self.db.execute("UPDATE approvals SET record=?,state=? WHERE id=?",
                            (json.dumps(rec),outcome,rec["id"]))
            if changed:
                prefix = "approval-update-" + rec["id"] + "-"
                self.db.execute("DELETE FROM outbox WHERE id LIKE ?", (prefix + "%",))
                event_id = prefix + uuid.uuid4().hex
                self.db.execute("INSERT INTO outbox VALUES (?, ?)", (event_id, json.dumps({
                    "id": event_id, "type": "approval.updated", "result": result})))
        log.info("Herdr answer verification: %s attempt=%s outcome=%s reason=%s elapsed_ms=%s",
                 rec["id"], result["attempt_id"], outcome, verification["reason"], verification["elapsed_ms"])
        return result

    def check(self, request_id, *, timeout=None):
        with self.lock:
            rec, state = self._record(request_id)
            if state not in CONSUMED_STATES:
                raise BridgeError("No approval attempt to check; nothing was sent")
            if request_id in self.active or state == "confirmed":
                return self._result(rec, state)
            if state == "reserved":
                try:
                    if rec.get("kind") == "codex_question":
                        self._live_question(rec, rec["dispatch"]["option"], rec["dispatch"].get("answer"))
                    else:
                        self._live_dialog(rec)
                    retryable = True
                except Exception:
                    retryable = False
                return self._store_verification(rec, {"outcome": "not_sent", "reason": "not_submitted",
                                                     "retryable": retryable, "elapsed_ms": 0})
            # Legacy/crashed records are queryable, never silently rearmed.
            rec["dispatch"].setdefault("attempt_id", uuid.uuid4().hex)
            self._save(rec, state)
            self.active.add(request_id)
        try:
            verification = self._confirm_answer(rec, timeout=timeout)
            with self.lock:
                self.active.discard(request_id)
                return self._store_verification(rec, verification)
        finally:
            with self.lock:
                self.active.discard(request_id)

    def retry(self, request_id, attempt_id):
        prepared = self.prepare_answer(request_id, retry_from=attempt_id)
        if prepared.pop("ready_to_dispatch"):
            return self.dispatch_reserved(request_id, prepared["attempt_id"])
        return prepared

    def reconcile_answers(self):
        with self.lock:
            rows = self.db.execute("SELECT record,state FROM approvals WHERE state IN "
                                   "('reserved','dispatching','dispatched','unconfirmed','uncertain')").fetchall()
        for raw, state in rows:
            rec = json.loads(raw)
            if (self.clock() < rec.get("followup_deadline", 0)
                    and rec.get("verification", {}).get("reason") != "agent_replaced"):
                self.check(rec["id"], timeout=self.confirmation_interval * 2)
            elif rec.get("followup_deadline") and not rec.get("watch_expired"):
                with self.lock:
                    rec, state = self._record(rec["id"])
                    if rec["id"] in self.active or state == "confirmed":
                        continue
                    rec["watch_expired"] = True
                    self._store_verification(rec, rec.get("verification") or {
                        "outcome": "not_sent" if state == "reserved" else "uncertain",
                        "reason": "not_observed", "elapsed_ms": 0})

    def bind_card(self, request_id, binding):
        # Called only by the authorized OpenClaw handler; no response URLs/tokens.
        fields = {k: binding[k] for k in ("channel", "accountId", "conversationId", "messageTs")}
        if (fields["channel"] != "slack" or not all(isinstance(v, str) and v for v in fields.values())
                or not fields["conversationId"].startswith("D")):
            raise BridgeError("Approval result cards require a Slack DM")
        with self.lock, self.db:
            self._record(request_id)
            self.db.execute("INSERT OR IGNORE INTO cards VALUES (?, ?)",
                            (request_id, json.dumps(fields, sort_keys=True)))
        return {"bound": request_id}

    def result_cards(self, request_id):
        with self.lock:
            rec, state = self._record(request_id)
            return {"result": self._result(rec, state), "cards": [json.loads(r[0]) for r in
                    self.db.execute("SELECT binding FROM cards WHERE request_id=?", (request_id,))]}

    def _confirm_answer(self, rec, *, timeout=None):
        """Observe only: a provider receipt is not proof the TUI accepted a key.

        Require two consistent observations that the original approval is gone.
        Never send more keys, even if the original dialog remains unchanged.
        """
        if rec.get("kind") == "codex_question":
            return self._confirm_question(rec, timeout=timeout)
        start = self.monotonic()
        deadline = start + (self.confirmation_timeout if timeout is None else timeout)
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
                advanced = (rec.get("sequence") is not None and seq is not None and seq > rec["sequence"])
                current = None
                if stable and not same_dialog:
                    if advanced and status in ("working", "idle", "done"):
                        current = ("approval_closed", seq, status)
                    elif advanced and status == "blocked" and live is not None:
                        current = ("next_dialog", seq, live.fingerprint)
                if current is not None and current == candidate:
                    return {"outcome": "confirmed", "reason": current[0],
                            "elapsed_ms": round((self.monotonic() - start) * 1000),
                            "observation": observation, "retryable": False}
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
                "observation": observation,
                "retryable": (reason == "dialog_still_open" and stable and status == "blocked"
                              and seq == rec.get("sequence"))}

    def _confirm_question(self, rec, *, timeout=None):
        start = self.monotonic()
        deadline = start + (self.confirmation_timeout if timeout is None else timeout)
        reason, retryable, seen = "not_observed", False, False
        while True:
            try:
                info = self._agent(rec["pane_id"])
                if identity(info) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
                snapshot = self._question_snapshot(info)
                if snapshot["thread_id"] != rec["question"]["thread_id"]:
                    reason = "agent_replaced"
                    break
                answer = snapshot["answers"].get(rec["question"]["id"])
                if answer is not None:
                    if answer != rec["dispatch"]["answer"]:
                        reason = "answered_differently"
                        break
                    if seen:
                        return {"outcome": "confirmed", "reason": "question_answered", "retryable": False,
                                "elapsed_ms": round((self.monotonic() - start) * 1000)}
                    seen, reason = True, "answer_observed_once"
                else:
                    seen = False
                    question = snapshot["pending"].get(rec["question"]["id"])
                    if not question or question_fingerprint(question) != rec["fingerprint"]:
                        reason = "question_expired"
                        break
                    reason = "question_still_pending"
                    try:
                        self._live_question(rec, rec["dispatch"]["option"], rec["dispatch"]["answer"])
                        retryable = True
                    except Exception:
                        retryable = False
            except Exception:
                reason, retryable, seen = "read_failed", False, False
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(self.confirmation_interval, remaining))
        return {"outcome": "unconfirmed" if reason == "question_still_pending" else "uncertain",
                "reason": reason, "retryable": retryable if reason == "question_still_pending" else False,
                "elapsed_ms": round((self.monotonic() - start) * 1000)}

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
