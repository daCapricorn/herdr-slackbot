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
from .channels import PaneChannels
from .agent_adapters import adapters
from .codex_session import AUTHORITY_ERROR

log = logging.getLogger(__name__)
CONSUMED_STATES = {"reserved", "dispatching", "dispatched", "confirmed", "unconfirmed", "uncertain"}
ATTENTION_EVENTS = {"approval.requested", "agent.blocked", "question.requested"}


class BridgeError(ValueError):
    pass


def require_session_authority(info):
    if info.get(AUTHORITY_ERROR):
        raise BridgeError("Agent session identity unavailable; nothing was sent")


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
    def __init__(self, client, state_path, *, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep,
                 confirmation_timeout=5.0, confirmation_interval=0.2, followup_timeout=120,
                 question_source=None, notification_delay=10.0):
        self.client, self.clock = client, clock
        self.monotonic, self.sleep = monotonic, sleep
        self.confirmation_timeout = confirmation_timeout
        self.confirmation_interval = confirmation_interval
        self.followup_timeout = followup_timeout
        self.question_source = question_source or CodexQuestionSource()
        self.notification_delay = max(0.0, float(notification_delay))
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
        self.adapters = adapters(self.db)
        self.channels = PaneChannels(self.db)
        self.db.commit()

    def pane_channels(self, account_id):
        panes = self.client.list_panes()
        tabs = {t["tab_id"]: t for w in self.client.list_workspaces()
                for t in self.client.list_tabs(w["workspace_id"])}
        agents = self.client.list_agents()
        by_pane = {a["pane_id"]: a for a in agents}
        metadata = []
        for pane in panes:
            agent = by_pane.get(pane["pane_id"])
            display = self._public_agent(agent, agents) if agent else pane
            metadata.append({**pane, "name": pane.get("name") or pane.get("label")
                             or tabs.get(pane.get("tab_id"), {}).get("label")
                             or display.get("name") or pane["pane_id"], "model": display.get("model")})
        with self.lock, self.db:
            return self.channels.reconcile(account_id, metadata)

    def channel_provisioned(self, binding_id, channel_id, team_id):
        if not isinstance(channel_id, str) or not channel_id.startswith(("C", "G")):
            raise BridgeError("Expected a Slack channel ID")
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if rec.get("channel_id") and (rec["channel_id"] != channel_id or rec.get("team_id") != team_id):
                raise BridgeError("Pane already has another channel binding")
            other = self.channels.from_channel(rec["account_id"], channel_id)
            if other and other["id"] != binding_id:
                raise BridgeError("Slack channel already belongs to another pane")
            rec.update(channel_id=channel_id, team_id=team_id, state="provisioning")
            return self.channels.save(rec)

    def channel_ready(self, binding_id, channel_name, topic):
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if not rec.get("channel_id"):
                raise BridgeError("Channel has not been created")
            rec.update(state="ready", channel_name=channel_name, topic=topic)
            return self.channels.save(rec)

    def channel_archived(self, binding_id):
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if not rec.get("closed"):
                raise BridgeError("Close the Herdr pane before archiving its channel")
            rec["state"] = "archived"
            return self.channels.save(rec)

    def pane_for_channel(self, account_id, conversation_id):
        with self.lock:
            return self.channels.from_channel(account_id, conversation_id)

    def route_event(self, event_id, account_id):
        with self.lock, self.db:
            row = self.db.execute("SELECT event FROM outbox WHERE id=?", (event_id,)).fetchone()
            if not row:
                return None
            event = json.loads(row[0])
            if event["type"] == "approval.updated":
                rec, _ = self._record(event["result"]["request_id"])
                return rec.get("delivery_route")
            if event.get("route"):
                return event["route"]
            a = event["agent"]
            route = self.channels.route(account_id, a["pane_id"], a.get("terminal_id"))
            if route:
                event["route"] = route
                self.db.execute("UPDATE outbox SET event=? WHERE id=?", (json.dumps(event), event_id))
                if event.get("request"):
                    rec, _ = self._record(event["request"]["id"])
                    rec["delivery_route"] = route
                    self.db.execute("UPDATE approvals SET record=? WHERE id=?", (json.dumps(rec), rec["id"]))
            return route

    def authorize_request(self, request_id, account_id, conversation_id):
        with self.lock:
            rec, _ = self._record(request_id)
            route = rec.get("delivery_route")
            if route:
                if route["accountId"] != account_id or route["conversationId"] != conversation_id:
                    raise BridgeError("Request belongs to a different pane channel")
            elif not isinstance(conversation_id, str) or not conversation_id.startswith("D"):
                raise BridgeError("Legacy requests can only be answered in Slack DM")
            return {"pane_id": rec["pane_id"]}

    def status(self):
        agents = self.client.list_agents()
        return [self._public_agent(a, agents) for a in agents]

    def _enrich_agent(self, info):
        adapter = self.adapters.get(info.get("agent"))
        return adapter.enrich(info, self.client) if adapter else info

    def _public_agent(self, info, agents=None, *, enrich=True):
        if enrich:
            info = self._enrich_agent(info)
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
            with self.lock:
                adapter = self.adapters.get(info.get("agent"))
                metadata_info = adapter.source.bound_info(info) if enrich and adapter else info
            result["model"] = session_model(metadata_info, unique_workspace=unique)
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
        return self._enrich_agent(info)

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
        if kind in ATTENTION_EVENTS:
            event["notify_after"] = event["at"] + self.notification_delay
        if kind in ("approval.requested", "question.requested"):
            # Keep display evidence after the delivery event is acknowledged.
            rec = fields["request"]
            context = {k: event[k] for k in ("type", "agent", "text", "summary") if k in event}
            context["request"] = {k: rec[k] for k in ("id", "options", "question") if k in rec}
            rec["card_context"] = context
            self.db.execute("UPDATE approvals SET record=? WHERE id=?", (json.dumps(rec), rec["id"]))
        self.db.execute("INSERT INTO outbox VALUES (?, ?)", (event["id"], json.dumps(event)))
        return event

    def _queue_conversation(self, event_id, info, message, agent=None):
        event = {"id": event_id, "type": "conversation.message", "at": self.clock(),
                 "agent": agent or self._public_agent(info, enrich=False), **message}
        self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
                        (event["id"], json.dumps(event, ensure_ascii=False)))

    def sync_status(self, pane):
        info = self._agent(pane)
        with self.lock:
            adapter = self.adapters.get(info.get("agent"))
            return adapter.source.diagnostic(info) if adapter else {"state": "unavailable", "reason": "Agent has no transcript adapter"}

    def bind_session(self, pane, thread_id):
        info = self._agent(pane)
        with self.lock, self.db:
            adapter = self.adapters.get(info.get("agent"))
            if not adapter:
                raise BridgeError("Agent has no session adapter")
            adapter.source.bind(info, thread_id)
        self.sync_conversation(info)
        return self.sync_status(pane)

    def sync_conversation(self, info):
        """Persist normalized conversation events and their source cursor atomically."""
        info = self._enrich_agent(info)
        adapter = self.adapters.get(info.get("agent"))
        if not adapter:
            return 0
        try:
            with self.lock, self.db:
                live = self._agent(info["pane_id"])
                if identity(live) != identity(info):
                    return 0
                agents = self.client.list_agents()
                unique = sum(a.get("agent") == info.get("agent") and a.get("cwd") == info.get("cwd") for a in agents) == 1
                public = None

                def enqueue(event_id, queued, message):
                    nonlocal public
                    if public is None:
                        public = self._public_agent(info, agents, enrich=False)
                    self._queue_conversation(event_id, queued, message, public)

                return adapter.source.sync(info, enqueue, unique_workspace=unique)
        except Exception:
            log.exception("%s transcript sync failed for %s", info.get("agent"), info.get("pane_id"))
            return 0

    def discover(self, info):
        """Recover already-blocked agents; repeat scans also catch a redrawn dialog."""
        info = self._enrich_agent(info)
        self.sync_conversation(info)
        questions = self.discover_questions(info)
        if info.get("agent_status") != "blocked":
            # A transcript's final append can lag the terminal's idle transition.
            # Reconcile its durable completion on periodic scans, once per answer.
            adapter = self.adapters.get(info.get("agent"))
            if adapter and info.get("agent_status") in ("idle", "done"):
                with self.lock:
                    streaming = adapter.source.diagnostic(info).get("state") == "syncing"
                if streaming:
                    text = self.client.read_agent_once(info["pane_id"], 160, source="visible")
                    fresh = self._agent(info["pane_id"])
                    if identity(fresh) == identity(info) and fresh.get("agent_status") in ("idle", "done"):
                        with self.lock, self.db:
                            if adapter.source.claim_completion(info):
                                self._queue("agent.done", info, text=text)
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
                   "created_at": self.clock(),
                   "options": options}
            self.db.execute("INSERT INTO approvals VALUES (?, ?, 'pending')", (request_id, json.dumps(rec)))
            self._queue("approval.requested" if options else "agent.blocked", info,
                        request=rec, text=screen)

    def _question_snapshot(self, info, agents=None):
        agents = self.client.list_agents() if agents is None else agents
        unique = sum(a.get("agent") == "codex" and a.get("cwd") == info.get("cwd") for a in agents) == 1
        try:
            with self.lock:
                question_info = self.adapters["codex"].source.bound_info(info)
        except Exception as exc:
            raise QuestionUnavailable(str(exc)) from exc
        return self.question_source.snapshot(question_info, unique_workspace=unique)

    def discover_questions(self, info):
        if info.get("agent") != "codex" or info.get(AUTHORITY_ERROR):
            return {}
        try:
            snapshot = self._question_snapshot(info)
        except QuestionUnavailable:
            return {}
        if not snapshot["pending"]:
            return {}
        screen = self.client.read_agent_once(info["pane_id"], 160, source="visible")
        fresh = self._agent(info["pane_id"])
        if fresh.get(AUTHORITY_ERROR) or identity(fresh) != identity(info):
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
                       "created_at": self.clock(),
                       "options": [{"index": i, "label": o} for i, o in enumerate(question["options"])]}
                self.db.execute("INSERT INTO approvals VALUES (?, ?, 'pending')", (rec["id"], json.dumps(rec)))
                self._queue("question.requested", info, request=rec, text=screen, summary=question["title"])
        return snapshot["pending"]

    def transition(self, transition):
        if transition.ended or not transition.info:
            return
        info = self._enrich_agent(transition.info)
        self.sync_conversation(info)
        if transition.status == "blocked":
            self.discover(info)
        elif transition.status in ("done", "idle") and transition.prev_status in ("working", "blocked"):
            text = self.client.read_agent_once(info["pane_id"], 160, source="visible")
            if identity(self._agent(info["pane_id"])) != identity(info):
                return
            with self.lock, self.db:
                adapter = self.adapters.get(info.get("agent"))
                if adapter and not adapter.source.claim_completion(info):
                    return
                self._queue("agent.done", info, text=text)

    def pending(self):
        with self.lock, self.db:
            live = {a["pane_id"]: self._enrich_agent(a) for a in self.client.list_agents()}
            for request_id, raw in self.db.execute("SELECT id, record FROM approvals WHERE state='pending'").fetchall():
                rec = json.loads(raw)
                info = live.get(rec["pane_id"])
                if info and info.get(AUTHORITY_ERROR):
                    self.db.execute("UPDATE approvals SET state='superseded' WHERE id=?", (request_id,))
                    continue
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
                "SELECT record FROM approvals WHERE state='pending'")]

    def events(self, account_id=None):
        self.pending()  # Retire cards whose pane has already advanced.
        with self.lock, self.db:
            ready = []
            for (raw,) in self.db.execute("SELECT event FROM outbox ORDER BY rowid").fetchall():
                queued = json.loads(raw)
                if account_id and queued["type"] != "approval.updated":
                    a = queued["agent"]
                    if not queued.get("route") and not self.channels.route(account_id, a["pane_id"], a.get("terminal_id")):
                        continue
                event = self._event_for_delivery(queued)
                if event:
                    ready.append(event)
                    if len(ready) == 20:
                        break  # Deferred attention cannot block ready completion/results.
            return ready

    def event_for_delivery(self, event_id):
        """Recheck one queued card immediately before its OpenClaw handoff."""
        with self.lock, self.db:
            row = self.db.execute("SELECT event FROM outbox WHERE id=?", (event_id,)).fetchone()
            return self._event_for_delivery(json.loads(row[0])) if row else None

    def _event_for_delivery(self, event):
        request = event.get("request")
        if not request:
            return event
        row = self.db.execute("SELECT state FROM approvals WHERE id=?", (request["id"],)).fetchone()
        if not row or row[0] != "pending":
            self.db.execute("DELETE FROM outbox WHERE id=?", (event["id"],))
            return None
        if event["type"] not in ATTENTION_EVENTS:
            return event
        # The deadline is persisted in JSON. Older queues use their discovery time.
        due = event.get("notify_after", event.get("at", request["created_at"]) + self.notification_delay)
        if self.clock() < due:
            return None
        live = self._attention_still_pending(request)
        if live is False:
            self.db.execute("UPDATE approvals SET state='superseded' WHERE id=? AND state='pending'", (request["id"],))
            self.db.execute("DELETE FROM outbox WHERE id=?", (event["id"],))
        return event if live is True else None

    def _attention_still_pending(self, request):
        """True=still needs attention, False=obsolete, None=read unavailable."""
        try:
            info = self._agent(request["pane_id"])
            if info.get(AUTHORITY_ERROR):
                return None
            if identity(info) != tuple(request["identity"]):
                return False
            if request.get("kind") == "codex_question":
                question = self._question_snapshot(info)["pending"].get(request["question"]["id"])
                fresh = self._agent(request["pane_id"])
                if fresh.get(AUTHORITY_ERROR):
                    return None
                if identity(fresh) != identity(info):
                    return False
                return bool(question and question_fingerprint(question) == request["fingerprint"])
            if info.get("agent_status") != "blocked" or info.get("state_change_seq") != request["sequence"]:
                return False
            screen = self.client.read_agent_once(request["pane_id"], 160, source="visible")
            dialog = current_dialog(screen, info.get("agent"))
            fresh = self._agent(request["pane_id"])
            if fresh.get(AUTHORITY_ERROR):
                return None
            return (identity(fresh) == tuple(request["identity"])
                    and fresh.get("agent_status") == "blocked" and fresh.get("state_change_seq") == request["sequence"]
                    and (dialog.fingerprint if dialog else None) == request["fingerprint"])
        except Exception:
            return None  # Do not send a possibly handled request on failed reads.

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
            rec["followup_deadline"] = self.clock() + self.followup_timeout
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
        info = self._agent(rec["pane_id"])
        require_session_authority(info)
        if (tuple(rec["identity"]) != identity(info) or info.get("agent_status") != "blocked"
                or rec.get("sequence") != info.get("state_change_seq")):
            raise BridgeError("Agent changed or is no longer blocked; nothing was sent")
        screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
        dialog = current_dialog(screen, info.get("agent"))
        if not dialog or dialog.fingerprint != rec["fingerprint"]:
            raise BridgeError("Dialog changed; nothing was sent")
        fresh = self._agent(rec["pane_id"])
        require_session_authority(fresh)
        if (identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked"
                or fresh.get("state_change_seq") != rec.get("sequence")):
            raise BridgeError("Agent changed before dispatch; nothing was sent")
        return dialog, fresh

    def _claim_pane(self, rec):
        if rec["pane_id"] in self.sending:
            raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent")
        self.sending[rec["pane_id"]] = rec["id"]

    def _live_question(self, rec, option, answer):
        info = self._agent(rec["pane_id"])
        require_session_authority(info)
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
            require_session_authority(fresh)
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
        rec["followup_deadline"] = self.clock() + self.followup_timeout
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
                     and rec["id"] not in self.active)
        return {"request_id": rec["id"], "kind": rec.get("kind", "approval"),
                "card_context": rec.get("card_context"),
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
                or not fields["conversationId"].startswith(("D", "C", "G"))):
            raise BridgeError("Approval result cards require a Slack conversation")
        with self.lock, self.db:
            self.authorize_request(request_id, fields["accountId"], fields["conversationId"])
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
                if info.get(AUTHORITY_ERROR):
                    reason, observation = "session_identity_unavailable", None
                    break
                if identity(info) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
                screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
                fresh = self._agent(rec["pane_id"])
                if fresh.get(AUTHORITY_ERROR):
                    reason, observation = "session_identity_unavailable", None
                    break
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
                if info.get(AUTHORITY_ERROR):
                    reason = "session_identity_unavailable"
                    break
                if identity(info) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
                snapshot = self._question_snapshot(info)
                fresh = self._agent(rec["pane_id"])
                if fresh.get(AUTHORITY_ERROR):
                    reason = "session_identity_unavailable"
                    break
                if identity(fresh) != tuple(rec["identity"]):
                    reason = "agent_replaced"
                    break
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
            require_session_authority(info)
            if info.get("agent_status") not in ("idle", "done"):
                raise BridgeError("Agent must be idle/done; use an exact approval request to answer a dialog")
            fresh = self._agent(pane)
            require_session_authority(fresh)
            if identity(fresh) != identity(info) or fresh.get("agent_status") not in ("idle", "done"):
                raise BridgeError("Agent changed before dispatch")
            self.client.prompt_agent(pane, text)
            return {"pane_id": pane, "outcome": "dispatched"}

    def close(self):
        self.db.close()
