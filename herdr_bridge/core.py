"""Domain events and exact, one-shot terminal replies. No channel SDK imports."""
from __future__ import annotations

import copy
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
from .codex_session import AUTHORITY_ERROR, SessionUnavailable
from .terminal_inputs import TerminalInputs, known_empty_composer
from .errors import BridgeError

log = logging.getLogger(__name__)
CONSUMED_STATES = {"reserved", "dispatching", "dispatched", "confirmed", "unconfirmed", "uncertain"}
ATTENTION_EVENTS = {"approval.requested", "agent.blocked", "question.requested"}


def require_session_authority(info):
    if info.get(AUTHORITY_ERROR):
        raise BridgeError("Agent session identity unavailable; nothing was sent", code="session_identity_unavailable")


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
        self.operations = {}  # exact terminal -> nonblocking probe/effect owner
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
        self.inputs = TerminalInputs(self.db, self.clock)
        for record in self.inputs.started():
            if not record.get("arbiter_released"):
                self.sending.setdefault(record["pane_id"], f"input:{record['seq']}")
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
            raise BridgeError("Expected a Slack channel ID", code="invalid_channel", outcome="not_sent")
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if rec.get("channel_id") and (rec["channel_id"] != channel_id or rec.get("team_id") != team_id):
                raise BridgeError("Pane already has another channel binding", code="channel_binding_conflict", outcome="not_sent")
            other = self.channels.from_channel(rec["account_id"], channel_id)
            if other and other["id"] != binding_id:
                raise BridgeError("Slack channel already belongs to another pane", code="channel_binding_conflict", outcome="not_sent")
            rec.update(channel_id=channel_id, team_id=team_id, state="provisioning")
            return self.channels.save(rec)

    def channel_ready(self, binding_id, channel_name, topic):
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if not rec.get("channel_id"):
                raise BridgeError("Channel has not been created", code="channel_not_created", outcome="not_sent")
            rec.update(state="ready", channel_name=channel_name, topic=topic)
            return self.channels.save(rec)

    def channel_archived(self, binding_id):
        with self.lock, self.db:
            rec = self.channels.get(binding_id)
            if not rec.get("closed"):
                raise BridgeError("Close the Herdr pane before archiving its channel", code="pane_not_closed", outcome="not_sent")
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
                    raise BridgeError("Request belongs to a different pane channel", code="request_channel_mismatch", outcome="not_sent")
            elif not isinstance(conversation_id, str) or not conversation_id.startswith("D"):
                raise BridgeError("Legacy requests can only be answered in Slack DM", code="request_channel_mismatch", outcome="not_sent")
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
                binding = adapter.source.binding_snapshot(info) if enrich and adapter else None
            metadata_info = adapter.source.bound_info(info, binding=binding) if enrich and adapter else info
            result["model"] = session_model(metadata_info, unique_workspace=unique)
        except Exception:
            result["model"] = None
        return result

    def _agent(self, pane):
        if not isinstance(pane, str) or ":p" not in pane:
            raise BridgeError("Use an exact pane ID, for example w4:p2", code="invalid_pane")
        info = self.client.get_agent(pane)
        if info.get("pane_id") != pane:
            raise BridgeError("Pane identity mismatch", code="pane_identity_mismatch")
        if not info.get("terminal_id"):
            raise BridgeError("Herdr did not provide a terminal identity", code="terminal_identity_unavailable")
        return self._enrich_agent(info)

    def read(self, pane, lines=120):
        info = self._agent(pane)
        # Visible reads do not scroll or disturb a blocked/working TUI.
        text = self.client.read_agent_once(pane, max(1, min(int(lines), 300)), source="visible")
        if identity(self._agent(pane)) != identity(info):
            raise BridgeError("Agent changed during read", code="agent_changed", outcome="not_sent")
        return {"agent": self._public_agent(info), "text": text}

    def _queue(self, kind, agent, **fields):
        event = {"id": uuid.uuid4().hex, "type": kind, "at": self.clock(),
                 "agent": agent, **fields}
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
                 "agent": agent or public_agent(info), **message}
        self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
                        (event["id"], json.dumps(event, ensure_ascii=False)))

    def _queue_completion(self, event_id, record):
        event = {"id": "completion:" + event_id, "type": "agent.done", "at": self.clock(),
                 "agent": record["agent"], "text": record["text"], "summary": record["text"],
                 "completion": {key: record[key] for key in
                    ("event_id", "source_key", "terminal_id", "session_id", "turn_id")}}
        self.db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
                        (event["id"], json.dumps(event, ensure_ascii=False)))

    def sync_status(self, pane):
        info = self._agent(pane)
        with self.lock:
            adapter = self.adapters.get(info.get("agent"))
            return adapter.source.diagnostic(info) if adapter else {"state": "unavailable", "reason": "Agent has no transcript adapter"}

    def bind_session(self, pane, thread_id):
        info = self._agent(pane)
        adapter = self.adapters.get(info.get("agent"))
        if not adapter:
            raise BridgeError("Agent has no session adapter", code="session_adapter_unavailable", outcome="not_sent")
        source = adapter.source
        with self.lock:
            binding = source.binding_snapshot(info)
            owner = self._claim_operation({"terminal_id": info["terminal_id"]})
            if not owner:
                raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent", code="pane_busy", outcome="not_sent")
        try:
            session = source.validate_binding(info, thread_id)
            fresh = self._agent(pane)
            with self.lock, self.db:
                if identity(fresh) != identity(info) or source.binding_snapshot(info) != binding:
                    raise BridgeError("Agent changed; nothing was sent", code="agent_changed", outcome="not_sent")
                source.apply_binding(info, session)
        finally:
            self._release_operation(owner)
        self.sync_conversation(info)
        return self.sync_status(pane)

    def _source_info(self, info):
        with self.lock:
            source = self.adapters[info["agent"]].source
            return source.evidence_snapshot(info)["info"]

    def sync_conversation(self, info):
        """Native reader is detached; cursor and all reducers commit by CAS."""
        info = self._enrich_agent(copy.deepcopy(info))
        adapter = self.adapters.get(info.get("agent"))
        if not adapter:
            return 0
        source = adapter.source
        batch = None
        try:
            live = self._agent(info["pane_id"])
            if identity(live) != identity(info):
                return 0
            agents = self.client.list_agents()
            unique = sum(a.get("agent") == info.get("agent") and a.get("cwd") == info.get("cwd") for a in agents) == 1
            public = self._public_agent(info, agents, enrich=False)
            with self.lock:
                evidence = source.evidence_snapshot(info)
            session = source._resolve(evidence["info"], unique)
            with self.lock:
                if source.binding_snapshot(info) != evidence["binding"]:
                    return 0
                snapshot = source.sync_snapshot(evidence, session)
            batch = source.read_sync_batch(snapshot)
            live = self._agent(info["pane_id"])
            if identity(live) != identity(info):
                return 0
            with self.lock, self.db:
                self.db.execute("BEGIN")
                scanned = source.apply_sync_batch(snapshot, batch,
                    lambda eid, queued, message: self._queue_conversation(eid, queued, message, public),
                    completion_agent=public)
                source.drain_completions(info, self._queue_completion)
                return scanned
        except Exception as exc:
            with self.lock, self.db:
                source._status(info, "unavailable", reason=str(exc) if isinstance(exc, SessionUnavailable) else f"Transcript read failed: {type(exc).__name__}")
                if batch is None:
                    source.drain_completions(info, self._queue_completion)
            log.exception("%s transcript sync failed for %s", info.get("agent"), info.get("pane_id"))
            return 0

    def discover(self, info):
        """Recover already-blocked agents; repeat scans also catch a redrawn dialog."""
        info = self._enrich_agent(info)
        self.sync_conversation(info)
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
        display = self._public_agent(info)
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
            self._queue("approval.requested" if options else "agent.blocked", display,
                        request=rec, text=screen)

    def _question_snapshot(self, info, agents=None):
        agents = self.client.list_agents() if agents is None else agents
        unique = sum(a.get("agent") == "codex" and a.get("cwd") == info.get("cwd") for a in agents) == 1
        try:
            with self.lock:
                binding = self.adapters["codex"].source.binding_snapshot(info)
            question_info = self.adapters["codex"].source.bound_info(info, binding=binding)
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
        display = self._public_agent(info)
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
                self._queue("question.requested", display, request=rec, text=screen, summary=question["title"])
        return snapshot["pending"]

    def transition(self, transition):
        if transition.ended or not transition.info:
            return
        info = self._enrich_agent(transition.info)
        self.sync_conversation(info)
        if transition.status == "blocked":
            self.discover(info)
        elif (transition.status in ("done", "idle") and transition.prev_status in ("working", "blocked")
              and info.get("agent") not in self.adapters):
            text = self.client.read_agent_once(info["pane_id"], 160, source="visible")
            if identity(self._agent(info["pane_id"])) != identity(info):
                return
            display = self._public_agent(info)
            with self.lock, self.db:
                self._queue("agent.done", display, text=text)

    def pending(self):
        with self.lock:
            rows = self.db.execute("SELECT id, record FROM approvals WHERE state='pending'").fetchall()
        live = {a["pane_id"]: self._enrich_agent(a) for a in self.client.list_agents()}
        obsolete = []
        for request_id, raw in rows:
            rec = json.loads(raw)
            info = live.get(rec["pane_id"])
            if info and info.get(AUTHORITY_ERROR):
                obsolete.append((request_id, raw))
                continue
            if rec.get("kind") == "codex_question" and info and identity(info) == tuple(rec["identity"]):
                try:
                    question = self._question_snapshot(info, list(live.values()))["pending"].get(rec["question"]["id"])
                except QuestionUnavailable:
                    continue
                if question and question_fingerprint(question) == rec["fingerprint"]:
                    continue
                obsolete.append((request_id, raw))
            elif (not info or identity(info) != tuple(rec["identity"])
                    or info.get("agent_status") != "blocked"
                    or info.get("state_change_seq") != rec.get("sequence")):
                obsolete.append((request_id, raw))
        with self.lock, self.db:
            for request_id, raw in obsolete:
                self.db.execute("UPDATE approvals SET state='superseded' WHERE id=? AND record=? AND state='pending'", (request_id, raw))
            return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM approvals WHERE state='pending'").fetchall()]

    def events(self, account_id=None):
        # Ready durable results take precedence. Background discovery handles
        # full pending maintenance; polling never synchronously scans all panes.
        with self.lock:
            candidates = []
            for (raw,) in self.db.execute("SELECT event FROM outbox ORDER BY rowid").fetchall():
                queued = json.loads(raw)
                if account_id and queued["type"] not in ("approval.updated", "input.updated"):
                    a = queued["agent"]
                    if not queued.get("route") and not self.channels.route(account_id, a["pane_id"], a.get("terminal_id")):
                        continue
                candidates.append(queued)
            ready = [e for e in candidates if e["type"] not in ATTENTION_EVENTS and not e.get("request")]
        if ready:
            return ready[:20]
        return [event for queued in candidates if (event := self.event_for_delivery(queued["id"]))][:20]

    def event_for_delivery(self, event_id):
        """Probe a detached card, then compare both request and outbox tokens."""
        with self.lock, self.db:
            row = self.db.execute("SELECT event FROM outbox WHERE id=?", (event_id,)).fetchone()
            if not row:
                return None
            raw = row[0]
            event = json.loads(raw)
            request = event.get("request")
            if not request:
                return event
            token = self.db.execute("SELECT record,state FROM approvals WHERE id=?", (request["id"],)).fetchone()
            if not token or token[1] != "pending":
                self.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
                return None
            if event["type"] not in ATTENTION_EVENTS:
                return event
            due = event.get("notify_after", event.get("at", request["created_at"]) + self.notification_delay)
            if self.clock() < due:
                return None
        live = self._attention_still_pending(json.loads(token[0]))
        with self.lock, self.db:
            current_event = self.db.execute("SELECT event FROM outbox WHERE id=?", (event_id,)).fetchone()
            current = self.db.execute("SELECT record,state FROM approvals WHERE id=?", (request["id"],)).fetchone()
            if current_event != (raw,) or current != token:
                return None
            if live is False:
                self.db.execute("UPDATE approvals SET state='superseded' WHERE id=?", (request["id"],))
                self.db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
            return event if live is True else None

    def _claim_operation(self, rec):
        terminal = rec.get("terminal_id") or rec["identity"][1]
        if terminal in self.operations:
            return None
        token = uuid.uuid4().hex
        self.operations[terminal] = token
        return (terminal, token)

    def _release_operation(self, owner):
        with self.lock:
            if owner and self.operations.get(owner[0]) == owner[1]:
                self.operations.pop(owner[0])

    def _request_binding(self, rec):
        adapter = self.adapters.get(rec["identity"][2])
        return adapter.source.binding_snapshot({"terminal_id": rec["identity"][1]}) if adapter else None

    def _same_request(self, rec, state, binding):
        return self._record(rec["id"]) == (rec, state) and self._request_binding(rec) == binding

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
                    raise BridgeError("Retry is stale or acceptance is unknown; check status first. Nothing was sent", code="stale_retry", details={"request_id": request_id}, outcome="not_sent")
                option = rec["dispatch"]["option"]
                answer_text = rec["dispatch"].get("answer") if option is None else None
            else:
                custom = rec.get("kind") == "codex_question" and answer_text is not None and option is None
                if custom:
                    if not isinstance(answer_text, str) or not answer_text.strip() or len(answer_text) > 8000:
                        raise BridgeError("Question answer must contain 1–8000 characters; nothing was sent", code="invalid_answer", details={"request_id": request_id}, outcome="not_sent")
                    answer_text = answer_text.strip()
                elif answer_text is not None or type(option) is not int or option not in {o["index"] for o in rec["options"]}:
                    raise BridgeError("Unsupported option; nothing was sent", code="invalid_option", details={"request_id": request_id}, outcome="not_sent")
                if state in CONSUMED_STATES:
                    if (option != rec.get("dispatch", {}).get("option")
                            or custom and answer_text != rec["dispatch"].get("answer")):
                        raise BridgeError("A different option was already submitted; nothing was sent", code="choice_conflict", details={"request_id": request_id}, outcome="not_sent")
                    return {**self._result(rec, state),
                            "outcome": "checking" if state == "reserved" else self._result(rec, state)["outcome"],
                            "ready_to_dispatch": state == "reserved"}
                if state != "pending":
                    raise BridgeError("Request was superseded; nothing was sent", code="request_superseded", details={"request_id": request_id}, outcome="not_sent")
            self._ensure_pane_available(rec)
            binding = self._request_binding(rec)
            owner = self._claim_operation(rec)
            if not owner:
                return {**self._result(rec, state), "outcome": "checking", "ready_to_dispatch": False}
        try:
            proof = self._reservation_proof(rec, option, answer_text)
            with self.lock, self.db:
                if not self._same_request(rec, state, binding):
                    current, current_state = self._record(request_id)
                    return {**self._result(current, current_state), "ready_to_dispatch": False}
                self._reserve(rec, option, answer_text, proof=proof)
            return {**self._result(rec, "reserved"), "outcome": "checking", "ready_to_dispatch": True}
        finally:
            self._release_operation(owner)

    def dispatch_reserved(self, request_id, attempt_id):
        with self.lock:
            rec, state = self._record(request_id)
            if attempt_id != rec.get("dispatch", {}).get("attempt_id"):
                raise BridgeError("Submission is stale; nothing was sent", code="stale_submission", details={"request_id": request_id, "attempt_id": attempt_id}, outcome="not_sent")
            if state != "reserved":
                return self._result(rec, state)
            binding = self._request_binding(rec)
            owner = self._claim_operation(rec)
            if not owner:
                return {**self._result(rec, state), "outcome": "checking"}
        try:
            plan, fresh = self._reservation_proof(rec, rec["dispatch"]["option"], rec["dispatch"].get("answer"))
            with self.lock:
                with self.db:
                    if not self._same_request(rec, state, binding):
                        current, current_state = self._record(request_id)
                        return self._result(current, current_state)
                    self._ensure_pane_available(rec)
                    rec["dispatch"].update(plan)
                    rec["dispatch"].update(started_at=self.clock(), focused=fresh.get("focused"), revision=fresh.get("revision"))
                    rec["followup_deadline"] = self.clock() + self.followup_timeout
                    self._save(rec, "dispatching")
                self._claim_pane(rec)
                self.active.add(request_id)
            return self._finish_dispatch(rec)
        finally:
            self._release_operation(owner)

    def _record(self, request_id):
        row = self.db.execute("SELECT record, state FROM approvals WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise BridgeError("Request is absent; nothing was sent", code="request_absent", details={"request_id": request_id})
        return json.loads(row[0]), row[1]

    def _save(self, rec, state):
        """Stage an approval update; the caller owns its transaction."""
        self.db.execute("UPDATE approvals SET record=?, state=? WHERE id=?",
                        (json.dumps(rec), state, rec["id"]))

    def _live_dialog(self, rec):
        info = self._agent(rec["pane_id"])
        require_session_authority(info)
        if (tuple(rec["identity"]) != identity(info) or info.get("agent_status") != "blocked"
                or rec.get("sequence") != info.get("state_change_seq")):
            raise BridgeError("Agent changed or is no longer blocked; nothing was sent", code="agent_changed", outcome="not_sent")
        screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
        dialog = current_dialog(screen, info.get("agent"))
        if not dialog or dialog.fingerprint != rec["fingerprint"]:
            raise BridgeError("Dialog changed; nothing was sent", code="dialog_changed", outcome="not_sent")
        fresh = self._agent(rec["pane_id"])
        require_session_authority(fresh)
        if (identity(fresh) != identity(info) or fresh.get("agent_status") != "blocked"
                or fresh.get("state_change_seq") != rec.get("sequence")):
            raise BridgeError("Agent changed before dispatch; nothing was sent", code="agent_changed", outcome="not_sent")
        return dialog, fresh

    def _claim_pane(self, rec):
        self._ensure_pane_available(rec)
        self.sending[rec["pane_id"]] = rec["id"]

    def _ensure_pane_available(self, rec):
        if rec["pane_id"] in self.sending:
            raise BridgeError("This pane is still processing another approval; wait and check status. Nothing was sent", code="pane_busy", outcome="not_sent")
        terminal = rec.get("terminal_id")
        if not terminal and rec.get("identity"):
            terminal = rec["identity"][1]
        if self.inputs.blocker({"terminal_id": terminal, "seq": rec.get("seq")}):
            raise BridgeError("This terminal has an unresolved prompt write; wait and check status. Nothing was sent", code="terminal_write_unresolved", outcome="not_sent")

    def _live_question(self, rec, option, answer):
        info = self._agent(rec["pane_id"])
        require_session_authority(info)
        if identity(info) != tuple(rec["identity"]):
            raise BridgeError("Agent changed; nothing was sent", code="agent_changed")
        try:
            snapshot = self._question_snapshot(info)
            question = snapshot["pending"].get(rec["question"]["id"])
            if not question or question_fingerprint(question) != rec["fingerprint"]:
                raise BridgeError("Question was answered, expired or changed; nothing was sent", code="question_changed")
            screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible")
            plan = input_plan(screen, question, option, answer, list(snapshot["pending"]))
            fresh = self._agent(rec["pane_id"])
            require_session_authority(fresh)
            if (identity(fresh) != identity(info) or fresh.get("state_change_seq") != info.get("state_change_seq")
                    or plan["mode"] == "prompt" and fresh.get("agent_status") not in ("working", "idle", "done", "blocked")):
                raise BridgeError("Question input changed or an approval is open; nothing was sent", code="question_changed")
            if plan["mode"] == "prompt":
                # Herdr can classify the collapsed async summary as blocked even
                # though the main composer is visible and empty. agent.prompt
                # rejects all blocked input; use one encoded pane submission only
                # after the same exact question/composer checks, never as a retry.
                plan["transport"] = "pane" if fresh.get("agent_status") == "blocked" else "agent"
            # Recheck structured evidence after reading the terminal.
            current = self._question_snapshot(fresh)["pending"].get(question["id"])
            if not current or question_fingerprint(current) != rec["fingerprint"]:
                raise BridgeError("Question changed before dispatch; nothing was sent", code="question_changed")
            return plan, fresh
        except QuestionUnavailable as exc:
            raise BridgeError(str(exc), code="question_unavailable") from exc

    def _reservation_proof(self, rec, option, answer_text=None):
        if rec.get("kind") == "codex_question":
            answer = answer_text if option is None else rec["question"]["options"][option]
            plan, fresh = self._live_question(rec, option, answer)
        else:
            dialog, fresh = self._live_dialog(rec)
            plan = {"keys": keys_for(dialog, option)}
        return plan, fresh

    def _reserve(self, rec, option, answer_text=None, *, proof):
        plan, fresh = proof
        self._ensure_pane_available(rec)
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
            with self.lock, self.db:
                current, current_state = self._record(rec["id"])
                if current.get("dispatch", {}).get("attempt_id") == rec["dispatch"]["attempt_id"]:
                    current["dispatch"].update({k: rec["dispatch"][k] for k in ("acknowledged_at", "error_type") if k in rec["dispatch"]})
                    self._save(current, current_state)
                    rec = current
            verification = self._confirm_answer(rec)
            with self.lock, self.db:
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
        """Stage verification and its outbox update in the caller's transaction."""
        current, state = self._record(rec["id"])
        if current.get("dispatch", {}).get("attempt_id") != rec.get("dispatch", {}).get("attempt_id") or state == "confirmed":
            return self._result(current, state)
        # Expiry is a monotonic same-attempt decision made by the locked
        # reconciliation reducer. Preserve it while merging current metadata.
        if rec.get("watch_expired"):
            current["watch_expired"] = True
        rec = current
        rec["verification"] = verification
        outcome = "reserved" if verification["outcome"] == "not_sent" else verification["outcome"]
        result = self._result(rec, outcome)
        # The outbox carries only the latest result. Repeated edits are harmless.
        signature = [outcome, verification["reason"], result["retry_token"], result["watching"]]
        changed = signature != rec.get("last_result_signature")
        rec["last_result_signature"] = signature
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
                raise BridgeError("No approval attempt to check; nothing was sent", code="approval_attempt_absent", details={"request_id": request_id}, outcome="not_sent")
            if request_id in self.active or state == "confirmed":
                return self._result(rec, state)
            binding = self._request_binding(rec)
            owner = self._claim_operation(rec)
            if not owner:
                return {**self._result(rec, state), "outcome": "checking"}
        try:
            if state == "reserved":
                try:
                    self._reservation_proof(rec, rec["dispatch"]["option"], rec["dispatch"].get("answer"))
                    retryable = True
                except Exception:
                    retryable = False
                with self.lock, self.db:
                    if not self._same_request(rec, state, binding):
                        current, current_state = self._record(request_id)
                        return self._result(current, current_state)
                    return self._store_verification(rec, {"outcome": "not_sent", "reason": "not_submitted", "retryable": retryable, "elapsed_ms": 0})
            with self.lock:
                with self.db:
                    current, current_state = self._record(request_id)
                    if (current_state not in CONSUMED_STATES or current_state == "confirmed"
                            or current.get("dispatch", {}).get("attempt_id") != rec.get("dispatch", {}).get("attempt_id")
                            or self._request_binding(current) != binding):
                        return self._result(current, current_state)
                    rec, state = current, current_state
                    rec["dispatch"].setdefault("attempt_id", uuid.uuid4().hex)
                    self._save(rec, state)
                self.active.add(request_id)
            verification = self._confirm_answer(rec, timeout=timeout)
            with self.lock, self.db:
                self.active.discard(request_id)
                return self._store_verification(rec, verification)
        finally:
            with self.lock:
                self.active.discard(request_id)
            self._release_operation(owner)

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
                with self.lock, self.db:
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
            raise BridgeError("Approval result cards require a Slack conversation", code="invalid_card_binding", outcome="not_sent")
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

    def _input_snapshot(self, info):
        require_session_authority(info)
        session = session_of(info)
        if not isinstance(session, str) or not session:
            raise BridgeError("Agent session identity unavailable; nothing was sent", code="session_identity_unavailable", outcome="not_sent")
        if info.get("agent") not in self.adapters:
            raise BridgeError("Only Codex and Claude panes accept remote prompts; nothing was sent", code="unsupported_agent", outcome="not_sent")
        return {"pane_id": info["pane_id"], "terminal_id": info["terminal_id"],
                "session_id": session, "agent_kind": info["agent"],
                "observed_status": info.get("agent_status"),
                "observed_sequence": info.get("state_change_seq")}

    def _input_binding(self, account_id, channel_id, expected=None):
        binding = self.channels.from_channel(account_id, channel_id)
        if (not binding or binding.get("closed") or binding.get("state") != "ready"
                or not binding.get("channel_id") or not binding.get("terminal_id")):
            raise BridgeError("This Slack channel is not a ready Herdr pane; nothing was sent", code="channel_not_ready", outcome="not_sent")
        if expected and any(binding.get(key) != expected.get(key)
                            for key in ("id", "pane_id", "terminal_id", "channel_id", "account_id")):
            raise BridgeError("Pane channel binding changed; nothing was sent", code="channel_binding_changed", outcome="not_sent")
        return binding

    def _native_input_blocker(self, info, rec):
        if rec["agent_kind"] != "codex":
            return None
        source = self.adapters["codex"].source
        with self.lock:
            previous = self.inputs.last_dispatched(rec)
            detached = source.evidence_snapshot(info)
            prefix = f"{rec['terminal_id']}:{session_of(info)}:"
            states = {key: (raw,) for key, raw in self.db.execute("SELECT source_key,record FROM codex_input_readiness WHERE substr(source_key,1,?)=?", (len(prefix), prefix)).fetchall()}
        deltas = {}
        try:
            blocker = source.input_blocker(detached["info"], previous, states=states, deltas=deltas)
            with self.lock, self.db:
                current = {key: (raw,) for key, raw in self.db.execute("SELECT source_key,record FROM codex_input_readiness WHERE substr(source_key,1,?)=?", (len(prefix), prefix)).fetchall()}
                if (current != states or self.inputs.last_dispatched(rec) != previous
                        or source.binding_snapshot(info) != detached["binding"]):
                    return "codex_native_readiness_unavailable"
                for key, raw in deltas.items():
                    self.db.execute("INSERT OR REPLACE INTO codex_input_readiness VALUES (?,?)", (key, raw))
            return blocker
        except Exception:
            return "codex_native_readiness_unavailable"

    def ingest_input(self, account_id, conversation_id, message_ts, sender_id, text,
                     thread_id=None, binding_id=None):
        """Deduplicate one authenticated Slack prompt and queue or submit it once."""
        if not all(isinstance(value, str) and value for value in
                   (account_id, conversation_id, message_ts, sender_id)):
            raise BridgeError("Slack input identity is incomplete; nothing was sent", code="invalid_input_identity", outcome="not_sent")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise BridgeError("Prompt must contain 1–16000 characters; nothing was sent", code="invalid_prompt", outcome="not_sent")
        text = text.strip()
        with self.lock, self.db:
            duplicate = self.inputs.find("slack", account_id, conversation_id, message_ts)
            if duplicate:
                return self._input_result(duplicate)
            binding = self._input_binding(account_id, conversation_id)
            if binding_id and binding["id"] != binding_id:
                raise BridgeError("Pane channel binding changed; nothing was sent", code="channel_binding_changed", outcome="not_sent")
        info = self._agent(binding["pane_id"])
        snapshot = self._input_snapshot(info)
        if snapshot["terminal_id"] != binding["terminal_id"]:
            raise BridgeError("Pane channel terminal identity changed; nothing was sent", code="terminal_identity_changed", outcome="not_sent")
        with self.lock, self.db:
            duplicate = self.inputs.find("slack", account_id, conversation_id, message_ts)
            if duplicate:
                return self._input_result(duplicate)
            if self._input_binding(account_id, conversation_id) != binding:
                raise BridgeError("Pane channel binding changed; nothing was sent", code="channel_binding_changed", outcome="not_sent")
            rec = self.inputs.insert({"source": "slack", "account_id": account_id,
                "channel_id": conversation_id, "message_ts": message_ts,
                "sender_id": sender_id, "thread_id": str(thread_id) if thread_id is not None else None,
                "binding": {key: binding.get(key) for key in
                    ("id", "account_id", "channel_id", "pane_id", "terminal_id", "team_id")},
                "prompt": text, **snapshot})
            if info.get("agent_status") == "blocked":
                rec = self.inputs.save(rec, "rejected", reason="agent_blocked_use_approval")
                return self.inputs.result(rec)
            if info.get("agent_status") == "working":
                rec = self.inputs.save(rec, "queued", reason="agent_busy")
                return self._input_result(rec)
            if info.get("agent_status") not in ("idle", "done"):
                rec = self.inputs.save(rec, "rejected", reason="agent_status_unknown")
                return self.inputs.result(rec)
        return self._dispatch_input(rec["seq"])

    def _revalidate_input(self, rec):
        if rec["source"] == "slack":
            with self.lock:
                self._input_binding(rec["account_id"], rec["channel_id"], rec["binding"])
        info = self._agent(rec["pane_id"])
        require_session_authority(info)
        if identity(info) != (rec["session_id"], rec["terminal_id"], rec["agent_kind"]):
            raise BridgeError("Agent terminal or session changed; nothing was sent", code="agent_identity_changed", outcome="not_sent")
        return info

    def _input_result(self, rec):
        """Stage input notification state and outbox; caller holds lock/transaction."""
        rec = self.inputs.get(rec["seq"])
        if rec["source"] == "slack" and rec["state"] == "queued" and not rec.get("notify_updates"):
            rec = self.inputs.save(rec, "queued", notify_updates=True)
        if (rec.get("source") == "slack" and rec.get("notify_updates")
                and rec["state"] in ("confirmed", "uncertain", "rejected")
                and rec.get("last_notified_state") != rec["state"]):
            result = self.inputs.result(rec)
            event = {"id": uuid.uuid4().hex, "type": "input.updated", "at": self.clock(),
                     "result": result, "route": {"channel": "slack",
                     "accountId": rec["account_id"], "conversationId": rec["channel_id"],
                     "to": f"channel:{rec['channel_id']}", "threadId": rec.get("thread_id"),
                     "team_id": rec.get("binding", {}).get("team_id")}}
            self.db.execute("INSERT INTO outbox VALUES (?,?)", (event["id"], json.dumps(event)))
            rec = self.inputs.save(rec, rec["state"], last_notified_state=rec["state"])
        return self.inputs.result(rec)

    def _probe_input(self, rec):
        info = self._revalidate_input(rec)
        source_info = self._source_info(info)
        status = info.get("agent_status")
        if status == "blocked":
            return {"state": "rejected", "reason": "agent_blocked_use_approval"}
        if status == "working":
            return {"state": "queued", "reason": "agent_busy"}
        if status not in ("idle", "done"):
            return {"state": "rejected", "reason": "agent_status_unknown"}
        blocker = self._native_input_blocker(info, rec)
        if blocker:
            return {"state": "queued", "reason": blocker}
        adapter = self.adapters[rec["agent_kind"]]
        try:
            baseline = adapter.source.receipt_baseline(self._source_info(info))
        except Exception as exc:
            if info.get("codex_live_identity"):
                raise  # A fresh native proof never permits an unavailable baseline.
            baseline = {"unavailable": type(exc).__name__}
        fresh = self._revalidate_input(rec)
        if baseline.get("native_identity"):
            current_baseline = adapter.source.receipt_baseline(self._source_info(fresh))
            if any(current_baseline.get(key) != baseline.get(key) for key in ("source_key", "path")):
                raise BridgeError("Codex native rollout path changed; nothing was sent", code="native_rollout_changed", outcome="not_sent")
        if (fresh.get("agent_status") not in ("idle", "done")
                or fresh.get("state_change_seq") != info.get("state_change_seq")):
            return {"state": "queued", "reason": "agent_changed_or_busy"}
        blocker = self._native_input_blocker(fresh, rec)
        if blocker:
            return {"state": "queued", "reason": blocker}
        # Transcript resolution can be slow enough for a draft or a
        # local native turn to appear. Recheck both before reserving.
        screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible", format="ansi")
        if not known_empty_composer(rec["agent_kind"], screen):
            return {"state": "rejected", "reason": "composer_not_known_empty"}
        if rec["agent_kind"] == "codex":
            fresh = self._revalidate_input(rec)
            if (fresh.get("agent_status") not in ("idle", "done")
                    or fresh.get("state_change_seq") != info.get("state_change_seq")):
                return {"state": "queued", "reason": "agent_changed_or_busy"}
            blocker = self._native_input_blocker(fresh, rec)
            if blocker:
                return {"state": "queued", "reason": blocker}
            # Native metadata reads can outlast a local composer edit.
            # Keep the final external read the exact ANSI draft guard.
            screen = self.client.read_agent_once(rec["pane_id"], 160, source="visible", format="ansi")
            if not known_empty_composer(rec["agent_kind"], screen):
                return {"state": "rejected", "reason": "composer_not_known_empty"}
        return {"baseline": baseline, "fresh": fresh, "binding": source_info["_bridge_binding"]}

    def _dispatch_input(self, seq):
        with self.lock, self.db:
            rec = self.inputs.get(seq)
            if not rec or rec["state"] != "queued":
                return self._input_result(rec) if rec else {"input_id": seq, "outcome": "rejected", "reason": "input_absent"}
            if not self.inputs.is_first(rec) or self.inputs.blocker(rec) or rec["pane_id"] in self.sending:
                return self._input_result(rec)
            previous = self.inputs.last_dispatched(rec)
            owner = self._claim_operation(rec)
            if not owner:
                return self._input_result(rec)
        try:
            try:
                proof = self._probe_input(rec)
            except BridgeError as exc:
                proof = {"state": "rejected", "reason": "revalidation_failed"}
                log.info("Herdr prompt rejected before terminal write: input=%s error=%s", rec["seq"], type(exc).__name__)
            except Exception as exc:
                proof = {"state": "rejected", "reason": "guard_unavailable"}
                log.info("Herdr prompt guard unavailable: input=%s error=%s", rec["seq"], type(exc).__name__)
            with self.lock:
                with self.db:
                    current = self.inputs.get(seq)
                    if current != rec or self.inputs.last_dispatched(rec) != previous:
                        return self._input_result(current)
                    if rec["source"] == "slack":
                        try:
                            self._input_binding(rec["account_id"], rec["channel_id"], rec["binding"])
                        except BridgeError:
                            proof = {"state": "rejected", "reason": "revalidation_failed"}
                    if not self.inputs.is_first(rec) or self.inputs.blocker(rec) or rec["pane_id"] in self.sending:
                        return self._input_result(current)
                    if "state" not in proof and self.adapters[rec["agent_kind"]].source.binding_snapshot(proof["fresh"]) != proof["binding"]:
                        return self._input_result(current)
                    if "state" in proof:
                        return self._input_result(self.inputs.save(current, proof["state"], reason=proof["reason"]))
                    self._ensure_pane_available({**rec, "id": f"input:{seq}"})
                    rec = self.inputs.save(current, "reserved", reason="terminal_write_reserved",
                        attempt_id=uuid.uuid4().hex, reserved_at=self.clock(),
                        dispatch_sequence=proof["fresh"].get("state_change_seq"), receipt_baseline=proof["baseline"])
                self._claim_pane({**rec, "id": f"input:{seq}"})
            return self._finish_input(rec)
        finally:
            self._release_operation(owner)

    def _finish_input(self, rec):
        try:
            try:
                self.client.prompt_agent(rec["pane_id"], rec["prompt"])
            except Exception as exc:
                rec = self._save_input_progress(rec["seq"], "uncertain", reason="terminal_write_outcome_unknown",
                                                error_type=type(exc).__name__)
                return self.inputs.result(rec)
            rec = self._save_input_progress(rec["seq"], "observing", reason="awaiting_exact_transcript_receipt",
                                            write_returned_at=self.clock())
            if rec["state"] == "confirmed" or rec.get("arbiter_released"):
                return self.inputs.result(rec)
            deadline = self.monotonic() + self.confirmation_timeout
            while True:
                try:
                    live = self._revalidate_started_input(rec)
                    baseline = rec.get("receipt_baseline", {})
                    if not baseline.get("unavailable") and self.adapters[rec["agent_kind"]].source.receipt_recorded(
                            self._source_info(live), baseline, rec["prompt"]):
                        rec = self._save_input_progress(rec["seq"], "confirmed", reason="exact_user_transcript_recorded",
                                                        confirmed_at=self.clock())
                        return self.inputs.result(rec)
                except Exception:
                    pass
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    rec = self._save_input_progress(rec["seq"], "uncertain", reason="exact_transcript_receipt_unavailable")
                    return self.inputs.result(rec)
                self.sleep(min(self.confirmation_interval, remaining))
        finally:
            with self.lock:
                rec = self.inputs.get(rec["seq"])
                if (rec.get("state") not in ("reserved", "observing", "uncertain")
                        or rec.get("arbiter_released")):
                    self._release_input_arbiter(rec)

    def _save_input_progress(self, seq, state, **fields):
        with self.lock, self.db:
            current = self.inputs.get(seq)
            if current["state"] == "confirmed" or current.get("arbiter_released"):
                return current
            current = self.inputs.save(current, state, **fields)
            self._input_result(current)
            return self.inputs.get(seq)

    def _input_target_info(self, rec):
        panes = self.client.list_panes()
        if (not isinstance(panes, list) or any(not isinstance(pane, dict)
                or not isinstance(pane.get("pane_id"), str) or ":p" not in pane["pane_id"]
                or not isinstance(pane.get("terminal_id"), str) or not pane["terminal_id"]
                for pane in panes)
                or len({pane["pane_id"] for pane in panes}) != len(panes)
                or len({pane["terminal_id"] for pane in panes}) != len(panes)):
            raise BridgeError("Terminal inventory unavailable or ambiguous", code="terminal_inventory_unavailable")
        targets = [pane for pane in panes if pane["terminal_id"] == rec["terminal_id"]]
        if not any(pane["pane_id"] == rec["pane_id"] and pane["terminal_id"] == rec["terminal_id"]
                   for pane in panes):
            with self.lock:
                self._release_input_arbiter(rec)
        if not targets:
            return None
        info = self._agent(targets[0]["pane_id"])
        require_session_authority(info)
        if info["terminal_id"] != rec["terminal_id"] or not all(identity(info)):
            raise BridgeError("Terminal changed or session identity unavailable", code="terminal_identity_changed")
        return info

    def _revalidate_started_input(self, rec):
        info = self._input_target_info(rec)
        if info is None or identity(info) != (rec["session_id"], rec["terminal_id"], rec["agent_kind"]):
            raise BridgeError("Agent terminal or session changed; nothing was sent", code="agent_identity_changed")
        return info

    def _input_identity_closed(self, rec, info):
        expected = (rec["session_id"], rec["terminal_id"], rec["agent_kind"])
        return info is None or identity(info) != expected

    def _release_input_arbiter(self, rec):
        owner = f"input:{rec['seq']}"
        if self.sending.get(rec["pane_id"]) == owner:
            self.sending.pop(rec["pane_id"])

    def _release_input_if_closed(self, rec, info):
        if not self._input_identity_closed(rec, info):
            return False
        with self.lock, self.db:
            current = self.inputs.get(rec["seq"])
            if current and current["state"] != "confirmed" and not current.get("arbiter_released"):
                current = self.inputs.save(current, "uncertain",
                    reason="target_identity_closed_unconfirmed", arbiter_released=True)
                self._release_input_arbiter(current)
                self._input_result(current)
        return True

    def reconcile_inputs(self):
        """Observe started attempts and release at most one FIFO item per terminal."""
        with self.lock:
            started = self.inputs.started()
        for rec in started:
            try:
                info = self._input_target_info(rec)
            except Exception:
                continue
            if self._release_input_if_closed(rec, info):
                continue
            baseline = rec.get("receipt_baseline", {})
            if baseline.get("unavailable"):
                continue
            try:
                if self.adapters[rec["agent_kind"]].source.receipt_recorded(self._source_info(info), baseline, rec["prompt"]):
                    with self.lock, self.db:
                        current = self.inputs.get(rec["seq"])
                        if current and current["state"] != "confirmed" and not current.get("arbiter_released"):
                            current = self.inputs.save(current, "confirmed", reason="exact_user_transcript_recorded")
                            self._release_input_arbiter(current)
                            self._input_result(current)
            except Exception:
                continue
        with self.lock:
            queued = self.inputs.queued()
        terminals = set()
        for rec in queued:
            if rec["terminal_id"] in terminals:
                continue
            terminals.add(rec["terminal_id"])
            self._dispatch_input(rec["seq"])

    def send(self, pane, text):
        """Compatibility facade for `/herdr send`, using the same safe arbiter."""
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise BridgeError("Prompt must contain 1–16000 characters", code="invalid_prompt", outcome="not_sent")
        info = self._agent(pane)
        snapshot = self._input_snapshot(info)
        with self.lock, self.db:
            rec = self.inputs.insert({"source": "command", "account_id": "local",
                "channel_id": pane, "message_ts": uuid.uuid4().hex, "sender_id": "authorized-command",
                "thread_id": None, "binding": None, "prompt": text.strip(), **snapshot})
            if info.get("agent_status") == "blocked":
                rec = self.inputs.save(rec, "rejected", reason="agent_blocked_use_approval")
                raise BridgeError("Agent is blocked; use an exact approval request. Nothing was sent", code="agent_blocked", outcome="not_sent")
            if info.get("agent_status") == "working":
                return self.inputs.result(self.inputs.save(rec, "queued", reason="agent_busy"))
            if info.get("agent_status") not in ("idle", "done"):
                rec = self.inputs.save(rec, "rejected", reason="agent_status_unknown")
                raise BridgeError("Agent status is not recognized as safe; nothing was sent", code="agent_status_unknown", outcome="not_sent")
        return self._dispatch_input(rec["seq"])

    def close(self):
        with self.lock:
            self.db.close()
