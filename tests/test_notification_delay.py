"""Notifications wait for unattended requests; terminal handling never waits."""
import json

import pytest

from herdr_bridge.core import Bridge
from herdr_bridge.questions import QuestionUnavailable
from test_codex_questions import COLLAPSED, QUESTION, QuestionSource
from test_openclaw_bridge import Client


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def create(tmp_path, kind="approval", *, clock=None, source=None, client=None, **kwargs):
    clock, client, source = clock or Clock(), client or Client(), source or QuestionSource()
    if kind != "question":
        source.pending.clear()
    if kind == "blocked":
        client.screen = "An unsupported dialog needs attention"
        client.info["agent"] = "pi"
    elif kind == "question":
        client.screen = COLLAPSED
        client.info.update(cwd="/project", agent_status="working")
    core = Bridge(client, tmp_path / "delay.sqlite", clock=clock, question_source=source,
                  confirmation_timeout=0, **kwargs)
    core.discover(client.info)
    return core, clock, client, source


@pytest.mark.parametrize("kind,event_type", [("approval","approval.requested"), ("blocked","agent.blocked"),
                                             ("question","question.requested")])
def test_unattended_attention_is_available_only_after_ten_seconds(tmp_path, kind, event_type):
    core, clock, client, _ = create(tmp_path, kind)
    try:
        token = core.pending()[0]["id"]
        assert core.events() == []  # The pending request remains answerable on PC/through commands.
        clock.now = 109.999
        core.discover(client.info)
        assert core.events() == []
        clock.now = 110
        events = core.events()
        assert [(e["type"], e["request"]["id"], e["notify_after"]) for e in events] == [(event_type,token,110)]
        assert not client.keys and not client.prompts
    finally:
        core.close()


@pytest.mark.parametrize("kind", ["approval", "blocked", "question"])
def test_handled_on_pc_during_grace_period_is_never_delivered(tmp_path, kind):
    core, clock, client, source = create(tmp_path, kind)
    try:
        clock.now = 109
        if kind == "question":
            source.pending.clear()
            source.answers[QUESTION["id"]] = "Purple"
        else:
            client.info.update(agent_status="working", state_change_seq=9)
        clock.now = 110
        assert core.events() == [] and core.pending() == []
        assert core.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert not client.keys and not client.prompts
    finally:
        core.close()


def test_changed_dialog_at_same_blocked_sequence_is_cancelled_and_new_request_waits(tmp_path):
    core, clock, client, _ = create(tmp_path)
    try:
        first = core.pending()[0]["id"]
        client.screen = client.screen.replace("Set-Content", "Remove-Item")
        clock.now = 110
        assert core.events() == []  # Even if the five-second scan has not discovered replacement yet.
        core.discover(client.info)
        second = core.pending()[0]["id"]
        assert second != first and core.events() == []
        clock.now = 120
        assert [e["request"]["id"] for e in core.events()] == [second]
    finally:
        core.close()


@pytest.mark.parametrize("kind", ["approval", "blocked", "question"])
def test_restart_preserves_original_deadline_and_acknowledgement(tmp_path, kind):
    core, clock, client, source = create(tmp_path, kind)
    core.close()
    clock.now = 108
    again = Bridge(client, tmp_path / "delay.sqlite", clock=clock, question_source=source)
    try:
        again.discover(client.info)
        assert again.events() == []
        clock.now = 110
        event = again.events()[0]
        assert event["notify_after"] == 110
        again.ack(event["id"])
        again.discover(client.info)
        assert again.events() == []
        assert not client.keys and not client.prompts
    finally:
        again.close()


@pytest.mark.parametrize("kind", ["approval", "question"])
def test_unavailable_live_evidence_defers_attention_without_resetting_delay(tmp_path, kind):
    core, clock, client, source = create(tmp_path, kind)
    try:
        clock.now = 110
        read, snapshot = client.read_agent_once, source.snapshot
        def unreadable(*a, **kw):
            raise QuestionUnavailable("temporary read failure")
        if kind == "question":
            source.snapshot = unreadable
        else:
            client.read_agent_once = unreadable
        assert core.events() == []
        assert core.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        client.read_agent_once, source.snapshot = read, snapshot
        clock.now = 111
        assert core.events()[0]["notify_after"] == 110
    finally:
        core.close()


def test_completion_and_result_updates_are_immediate_despite_a_large_delayed_queue(tmp_path):
    core, _, client, _ = create(tmp_path)
    try:
        # At-least-once duplicate attention events must not occupy the entire ready batch.
        with core.db:
            raw = core.db.execute("SELECT event FROM outbox").fetchone()[0]
            for n in range(25):
                event = json.loads(raw)
                event["id"] = f"copy-{n}"
                core.db.execute("INSERT INTO outbox VALUES (?,?)", (event["id"],json.dumps(event)))
            core._queue("agent.done", client.info, text="finished")
            update = {"id":"result", "type":"approval.updated", "result":{"request_id":"x"}}
            core.db.execute("INSERT INTO outbox VALUES (?,?)", (update["id"],json.dumps(update)))
        assert [e["type"] for e in core.events()] == ["agent.done", "approval.updated"]
    finally:
        core.close()


def test_immediate_pre_handoff_check_cancels_a_card_answered_after_batch_read(tmp_path):
    core, clock, client, _ = create(tmp_path)
    try:
        clock.now = 110
        event = core.events()[0]
        client.info.update(agent_status="working", state_change_seq=9)
        assert core.event_for_delivery(event["id"]) is None
        assert core.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert not client.keys
    finally:
        core.close()


def test_request_remains_pending_beyond_old_expiry_and_obeys_attention_delay(tmp_path):
    core, clock, _, _ = create(tmp_path)
    try:
        clock.now = 110
        assert len(core.events()) == 1 and len(core.pending()) == 1
    finally:
        core.close()


def test_pre_upgrade_outbox_uses_existing_discovery_time_for_delay(tmp_path):
    core, clock, _, _ = create(tmp_path)
    try:
        event = json.loads(core.db.execute("SELECT event FROM outbox").fetchone()[0])
        del event["notify_after"]
        with core.db:
            core.db.execute("UPDATE outbox SET event=?", (json.dumps(event),))
        clock.now = 109
        assert core.events() == []
        clock.now = 110
        assert core.events()[0]["id"] == event["id"]
    finally:
        core.close()
