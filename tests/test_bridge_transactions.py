"""Exercise Bridge transaction ownership with durable, independent readback."""
import json
import sqlite3

import pytest

from herdr_bridge.core import Bridge
from herdr_bridge.claude_sync import ClaudeTranscriptSync
from test_claude_review_regressions import TID, append, answer, user
from test_openclaw_bridge import Client, request
from test_terminal_inputs import InputClient, ingest, make_bridge


class LateFailure(RuntimeError):
    pass


def stored(path, sql, args=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, args).fetchall()


@pytest.mark.parametrize("helper", ["save", "verification"])
def test_approval_helpers_leave_commit_and_rollback_to_caller(tmp_path, helper):
    path = tmp_path / "bridge.sqlite"
    client = Client()
    bridge = Bridge(client, path, confirmation_timeout=0)
    try:
        token = request(client, bridge)
        original = stored(path, "SELECT record,state FROM approvals WHERE id=?", (token,))
        outbox = stored(path, "SELECT id,event FROM outbox ORDER BY rowid")
        with pytest.raises(LateFailure), bridge.lock, bridge.db:
            rec, _ = bridge._record(token)
            rec["late_change"] = True
            if helper == "save":
                bridge._save(rec, "reserved")
            else:
                bridge._store_verification(rec, {"outcome": "uncertain", "reason": "not_observed",
                                                  "elapsed_ms": 0})
            raise LateFailure("caller failed after helper returned")
        assert stored(path, "SELECT record,state FROM approvals WHERE id=?", (token,)) == original
        assert stored(path, "SELECT id,event FROM outbox ORDER BY rowid") == outbox
    finally:
        bridge.close()


def test_input_result_leaves_state_and_notification_rollback_to_caller(tmp_path):
    client, bridge = make_bridge(tmp_path, status="working")
    path = tmp_path / "bridge.sqlite"
    try:
        seq = ingest(bridge, "100.1")["input_id"]
        original = stored(path, "SELECT state,record FROM terminal_inputs WHERE seq=?", (seq,))
        with pytest.raises(LateFailure), bridge.lock, bridge.db:
            rec = bridge.inputs.save(bridge.inputs.get(seq), "confirmed", reason="exact_user_transcript_recorded")
            bridge._input_result(rec)
            raise LateFailure("caller failed after notification was staged")
        assert stored(path, "SELECT state,record FROM terminal_inputs WHERE seq=?", (seq,)) == original
        assert stored(path, "SELECT id FROM outbox") == []
        assert client.prompts == []
    finally:
        bridge.close()


def test_sync_source_savepoints_do_not_commit_before_completion_drain(tmp_path, monkeypatch):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    client = InputClient()
    client.info["agent_session"] = {"value": TID}
    state = tmp_path / "bridge.sqlite"
    bridge = Bridge(client, state)
    source = ClaudeTranscriptSync(bridge.db, home=tmp_path)
    bridge.adapters["claude"].source = source
    try:
        bridge.sync_conversation(client.info)
        original = stored(state, "SELECT * FROM claude_sync_cursors")
        append(path, [user("new question", "u1"), answer("new answer", "a1"),
                      {"type": "system", "subtype": "turn_duration"}])
        drain = source.drain_completions

        def fail_after_drain(*args):
            drain(*args)
            raise LateFailure("late failure after completion was staged")

        monkeypatch.setattr(source, "drain_completions", fail_after_drain)
        assert bridge.sync_conversation(client.info) == 0
        assert stored(state, "SELECT * FROM claude_sync_cursors") == original
        for table in ("outbox", "claude_sync_seen", "claude_completions", "claude_completion_seen"):
            assert stored(state, f"SELECT * FROM {table}") == []
        monkeypatch.setattr(source, "drain_completions", drain)
        assert bridge.sync_conversation(client.info) > 0
        events = [json.loads(row[0]) for row in stored(state, "SELECT event FROM outbox")]
        assert [event["type"] for event in events].count("conversation.message") == 2
        assert [event["type"] for event in events].count("agent.done") == 1
    finally:
        bridge.close()


@pytest.mark.parametrize("kind", ["approval", "input"])
def test_result_outbox_failure_rolls_back_verification_state(tmp_path, kind):
    path = tmp_path / "bridge.sqlite"
    if kind == "approval":
        client = Client()
        bridge = Bridge(client, path, confirmation_timeout=0)
        token = request(client, bridge)
        bridge.prepare_answer(token, option=0)
        table, key, value = "approvals", "id", token
        update_type = "approval.updated"
        operation = lambda: bridge.check(token)
    else:
        client, bridge = make_bridge(tmp_path, status="working")
        seq = ingest(bridge, "100.1")["input_id"]
        table, key, value = "terminal_inputs", "seq", seq
        update_type = "input.updated"
        operation = lambda: bridge._save_input_progress(seq, "confirmed", reason="exact_user_transcript_recorded")
    try:
        original = stored(path, f"SELECT state,record FROM {table} WHERE {key}=?", (value,))
        with bridge.lock, bridge.db:
            bridge.db.execute(f"""CREATE TRIGGER fail_result_outbox BEFORE INSERT ON outbox
                WHEN json_extract(NEW.event, '$.type') = '{update_type}'
                BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
            operation()
        assert stored(path, f"SELECT state,record FROM {table} WHERE {key}=?", (value,)) == original
        assert not bridge.db.in_transaction
    finally:
        bridge.close()


@pytest.mark.parametrize("kind", ["approval", "input"])
def test_reservation_is_durable_before_terminal_mutation_and_unknown_write_is_not_replayed(tmp_path, kind):
    path = tmp_path / "bridge.sqlite"
    if kind == "approval":
        client = Client()
        bridge = Bridge(client, path, confirmation_timeout=0)
        token = request(client, bridge)
        prepared = bridge.prepare_answer(token, option=0)
        assert stored(path, "SELECT state FROM approvals WHERE id=?", (token,)) == [("reserved",)]
        table, key, value, expected = "approvals", "id", token, "dispatching"
        operation = lambda: bridge.dispatch_reserved(token, prepared["attempt_id"])
        method = "send_keys"
    else:
        client, bridge = make_bridge(tmp_path, status="idle")
        table, key, value, expected = "terminal_inputs", "message_ts", "100.1", "reserved"
        operation = lambda: ingest(bridge, "100.1")
        method = "prompt_agent"
    calls = []
    observations = []

    def unknown_write(*args):
        calls.append(args)
        state, raw = stored(path, f"SELECT state,record FROM {table} WHERE {key}=?", (value,))[0]
        rec = json.loads(raw)
        observations.append((bridge.db.in_transaction, state,
                             bool((rec["dispatch"] if kind == "approval" else rec).get("attempt_id"))))
        raise OSError("terminal write acknowledgement lost")

    setattr(client, method, unknown_write)
    try:
        operation()
        operation()
        assert len(calls) == 1
        assert observations == [(False, expected, True)]
        reopened = Bridge(client, path, confirmation_timeout=0)
        try:
            if kind == "approval":
                reopened.dispatch_reserved(token, prepared["attempt_id"])
            else:
                ingest(reopened, "100.1")
            assert len(calls) == 1
        finally:
            reopened.close()
    finally:
        bridge.close()


@pytest.mark.parametrize("kind", ["dispatch", "check", "input"])
def test_failed_commit_does_not_leak_operation_ownership(tmp_path, kind):
    path = tmp_path / "bridge.sqlite"
    if kind == "input":
        client, bridge = make_bridge(tmp_path, status="working")
        seq = ingest(bridge, "100.1")["input_id"]
        client.info.update(agent_status="idle", state_change_seq=2)
        table, key, value = "terminal_inputs", "seq", seq
        target, original = "reserved", "queued"
        operation = lambda: bridge._dispatch_input(seq)
        effects = client.prompts
    else:
        client = Client()
        bridge = Bridge(client, path, confirmation_timeout=0, notification_delay=0)
        token = request(client, bridge)
        prepared = bridge.prepare_answer(token, option=0)
        if kind == "check":
            bridge.dispatch_reserved(token, prepared["attempt_id"])
        table, key, value = "approvals", "id", token
        target, original = ("dispatching", "reserved") if kind == "dispatch" else ("unconfirmed", "unconfirmed")
        operation = (lambda: bridge.dispatch_reserved(token, prepared["attempt_id"])) if kind == "dispatch" else (lambda: bridge.check(token))
        effects = client.keys
    try:
        bridge.db.execute("PRAGMA foreign_keys=ON")
        with bridge.lock, bridge.db:
            bridge.db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            bridge.db.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
            bridge.db.execute(f"""CREATE TRIGGER fail_commit AFTER UPDATE OF state ON {table}
                WHEN NEW.state='{target}' BEGIN INSERT INTO child VALUES(1); END""")
        before = len(effects)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            operation()
        assert stored(path, f"SELECT state FROM {table} WHERE {key}=?", (value,)) == [(original,)]
        assert len(effects) == before
        assert not bridge.active and not bridge.sending and not bridge.db.in_transaction
        with bridge.lock, bridge.db:
            bridge.db.execute("DROP TRIGGER fail_commit")
        operation()
        assert not bridge.active and not bridge.sending
        assert len(effects) == before + (kind != "check")
    finally:
        bridge.close()
