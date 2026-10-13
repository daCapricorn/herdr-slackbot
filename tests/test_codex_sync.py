import json
import os
import sqlite3
import pytest

from herdr_bridge.codex_sync import CodexRolloutSync


def row(kind, payload, *, ordinal=None):
    value = {"type": kind, "payload": payload}
    if ordinal is not None:
        value["ordinal"] = ordinal
    return value


def write_rows(path, rows):
    with path.open("ab") as stream:
        for value in rows:
            stream.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")


def setup_source(tmp_path, rows):
    home = tmp_path / "codex"
    home.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    write_rows(rollout, [row("session_meta", {"id": "session-1", "cwd": "/project"})] + rows)
    state = sqlite3.connect(home / "state_1.sqlite")
    state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, agent_path TEXT)")
    state.execute("INSERT INTO threads VALUES ('session-1', ?, '/project', 0, NULL)", (str(rollout),))
    state.commit()
    bridge_db = sqlite3.connect(tmp_path / "bridge.sqlite")
    bridge_db.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY, event TEXT NOT NULL)")
    source = CodexRolloutSync(bridge_db, home=home)
    info = {"agent": "codex", "pane_id": "w1:p1", "terminal_id": "terminal-1", "cwd": "/project",
            "agent_session": {"value": "session-1"}, "agent_status": "working"}
    return source, bridge_db, state, rollout, info


def collect(db):
    return [(row_id, json.loads(raw)) for row_id, raw in db.execute("SELECT id,event FROM outbox ORDER BY rowid")]


def test_sync_starts_at_latest_turn_and_sends_only_user_final_and_tool_summary(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [
        row("event_msg", {"type": "task_started", "turn_id": "old"}, ordinal=1),
        row("event_msg", {"type": "item_completed", "turn_id": "old", "item": {"type": "UserMessage", "id": "old-user",
            "content": [{"type": "text", "text": "old prompt"}]}}, ordinal=2),
        row("event_msg", {"type": "task_started", "turn_id": "current"}, ordinal=3),
        row("event_msg", {"type": "item_completed", "turn_id": "current", "item": {"type": "UserMessage", "id": "user-1",
            "content": [{"type": "text", "text": "Fix it"}]}}, ordinal=4),
        # A matching model-facing response_item is not emitted as a second message.
        row("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix it"}]}, ordinal=4.5),
        row("event_msg", {"type": "item_completed", "turn_id": "current", "item": {
            "type": "CommandExecution", "id": "cmd-1", "command": ["pytest", "-q"], "stdout": "private output"}}, ordinal=5),
        row("event_msg", {"type": "item_completed", "turn_id": "current", "item": {
            "type": "AgentMessage", "id": "answer-1", "phase": "final_answer",
            "content": [{"type": "Text", "text": "Fixed it."}]}}, ordinal=6),
        row("response_item", {"type": "message", "role": "assistant", "id": "answer-1", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Fixed it."}]}, ordinal=7),
    ])
    def enqueue(event_id, agent, message):
        db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
                   (event_id, json.dumps({"id": event_id, "type": "conversation.message", "agent": agent, **message})))
    source.sync(info, enqueue)
    events = [event for _, event in collect(db)]
    assert [(event["role"], event["text"]) for event in events] == [
        ("user", "Fix it"), ("tool", "Ran: `pytest -q`"), ("assistant", "Fixed it.")]
    assert "private output" not in json.dumps(events)
    assert len({event["id"] for event in events}) == 3
    state.close()


def test_cursor_survives_restart_and_incomplete_lines_are_not_consumed(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [
        row("event_msg", {"type": "task_started", "turn_id": "t1"}, ordinal=1),
        row("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u1",
            "content": [{"type": "text", "text": "first"}]}}, ordinal=2),
    ])
    enqueue = lambda event_id, agent, message: db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
        (event_id, json.dumps({"type": "conversation.message", "agent": agent, **message})))
    source.sync(info, enqueue)
    before = len(collect(db))
    pending = json.dumps(row("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u2",
        "content": [{"type": "text", "text": "second"}]}}, ordinal=3)).encode()
    with rollout.open("ab") as stream:
        stream.write(pending[:40])
    source.sync(info, enqueue)
    assert len(collect(db)) == before
    with rollout.open("ab") as stream:
        stream.write(pending[40:] + b"\n")
    source.sync(info, enqueue)
    assert [event["text"] for _, event in collect(db)] == ["first", "second"]
    restarted = CodexRolloutSync(db, home=source.home)
    restarted.sync(info, enqueue)
    assert [event["text"] for _, event in collect(db)] == ["first", "second"]
    state.close()


def test_cursor_replacement_does_not_requeue_already_acknowledged_messages(tmp_path):
    rows = [row("event_msg", {"type": "task_started", "turn_id": "t1"}, ordinal=1),
            row("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u1",
                "content": [{"type": "text", "text": "once"}]}}, ordinal=2)]
    source, db, state, rollout, info = setup_source(tmp_path, rows)
    enqueue = lambda event_id, agent, message: db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
        (event_id, json.dumps({"role": message["role"], "text": message["text"]})))
    source.sync(info, enqueue)
    db.execute("DELETE FROM outbox")  # simulate Slack delivery and ack
    replacement = rollout.with_suffix(".replacement")
    write_rows(replacement, [row("session_meta", {"id": "session-1", "cwd": "/project"})] + rows + [row("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u2",
        "content": [{"type": "text", "text": "twice"}]}}, ordinal=3)])
    os.replace(replacement, rollout)
    source.sync(info, enqueue)
    assert [json.loads(raw)["text"] for (raw,) in db.execute("SELECT event FROM outbox")] == ["twice"]
    state.close()


def test_paginated_rollout_user_message_turn_items_are_synced(tmp_path):
    # Current Codex rollouts persist user inputs as completed TurnItems.
    source, db, state, _, info = setup_source(tmp_path, [
        row("event_msg", {"type": "turn_started", "turn_id": "t1"}, ordinal=1),
        row("event_msg", {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "Summarize the diff"}]}}, ordinal=2),
    ])
    source.sync(info, lambda event_id, agent, message: db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
        (event_id, json.dumps({"role": message["role"], "text": message["text"]}))))
    assert [json.loads(raw)["text"] for (raw,) in db.execute("SELECT event FROM outbox")] == ["Summarize the diff"]
    state.close()


def test_prompt_receipt_requires_exact_native_user_record_after_baseline(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [
        row("event_msg", {"type":"user_message","message":"same prompt"})])
    baseline = source.receipt_baseline(info)
    assert not source.receipt_recorded(info,baseline,"same prompt")
    write_rows(rollout,[row("event_msg", {"type":"item_completed","item":{
        "type":"UserMessage","id":"new","content":[{"type":"text","text":"different"}]}})])
    assert not source.receipt_recorded(info,baseline,"same prompt")
    write_rows(rollout,[row("event_msg", {"type":"user_message","message":"same prompt"})])
    assert source.receipt_recorded(info,baseline,"same prompt")
    state.close()


def test_ignores_tool_output_async_messages_and_nonfinal_assistant_text(tmp_path):
    source, db, state, _, info = setup_source(tmp_path, [
        row("event_msg", {"type": "task_started", "turn_id": "t"}, ordinal=1),
        row("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>private</system-reminder>"}]}, ordinal=2),
        row("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "id": "progress", "phase": "commentary", "text": "working"}}, ordinal=3),
        row("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "id": "async", "delivery": "async", "text": "question"}}, ordinal=4),
    ])
    source.sync(info, lambda *args: db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
        (args[0], json.dumps({"id": args[0], "role": args[2]["role"], "text": args[2]["text"]}))))
    assert collect(db) == []
    state.close()


def test_tool_summary_redacts_inline_credentials_and_never_includes_output(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [
        row("event_msg", {"type": "task_started", "turn_id": "t"}, ordinal=1),
        row("event_msg", {"type": "item_completed", "item": {"type": "CommandExecution", "id": "cmd",
            "command": ["env", "SLACK_BOT_TOKEN=xoxb-secret", "run"], "stdout": "secret output"}}, ordinal=2),
    ])
    source.sync(info, lambda event_id, agent, message: db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)",
        (event_id, json.dumps({"role": message["role"], "text": message["text"]}))))
    body = json.dumps(collect(db))
    assert "xoxb-secret" not in body and "secret output" not in body
    assert "[redacted]" in body
    state.close()


def test_missing_session_resolves_only_with_unique_pane_and_thread(tmp_path):
    source, db, state, _, info = setup_source(tmp_path, [])
    info.pop("agent_session")
    source.sync(info, lambda *args: None)
    assert source.diagnostic(info)["state"] == "unavailable"
    source.sync(info, lambda *args: None, unique_workspace=True)
    assert source.diagnostic(info)["thread_id"] == "session-1"
    assert db.execute("SELECT source_key FROM codex_sync_cursors").fetchone()[0] == "terminal-1:session-1"
    state.execute("INSERT INTO threads VALUES ('other', 'missing', '/project', 0, NULL)")
    state.commit()
    source.sync(info, lambda *args: None, unique_workspace=True)
    assert "ambiguous" in source.diagnostic(info)["reason"]


def test_explicit_binding_survives_restart_and_rejects_changed_identity(tmp_path):
    source, db, state, _, info = setup_source(tmp_path, [])
    info.pop("agent_session")
    source.bind(info, "session-1")
    db.commit()
    restarted = CodexRolloutSync(db, home=source.home)
    restarted.sync(info, lambda *args: None)
    assert restarted.diagnostic(info)["thread_id"] == "session-1"
    assert restarted.bound_info(info)["agent_session"]["value"] == "session-1"
    # A recreated terminal does not inherit the previous terminal's binding.
    changed = {**info, "terminal_id": "terminal-2"}
    restarted.sync(changed, lambda *args: None)
    assert restarted.diagnostic(changed)["state"] == "unavailable"
    restarted.sync({**info, "agent_session": {"value": "other"}}, lambda *args: None)
    assert "Session not found" in restarted.diagnostic(info)["reason"]
    restarted.sync({**info, "cwd": "/different"}, lambda *args: None)
    assert "directory changed" in restarted.diagnostic(info)["reason"]


def test_rollout_identity_mismatch_never_queues_messages(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    rollout.write_text(json.dumps(row("session_meta", {"id": "wrong", "cwd": "/project"})) + "\n")
    source.sync(info, lambda *args: db.execute("INSERT INTO outbox VALUES ('bad','bad')"))
    assert collect(db) == []
    assert "identity" in source.diagnostic(info)["reason"]


def test_settled_first_scan_records_baseline_then_syncs_next_turn_once(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    info.pop("agent_session")
    info["agent_status"] = "idle"
    enqueue = lambda event_id, agent, message: db.execute("INSERT INTO outbox VALUES (?, ?)",
        (event_id, json.dumps(message)))
    source.sync(info, enqueue, unique_workspace=True)
    write_rows(rollout, [row("event_msg", {"type": "task_started"}, ordinal=1),
        row("event_msg", {"type": "user_message", "message": "new turn"}, ordinal=2)])
    source.sync(info, enqueue, unique_workspace=True)
    source.sync(info, enqueue, unique_workspace=True)
    assert [event["text"] for _, event in collect(db)] == ["new turn"]


def test_multiline_command_summary_is_single_line_inline_code():
    from herdr_bridge.codex_sync import _tool_summary
    summary = _tool_summary({"type": "CommandExecution", "command": "python - <<'PY'\nprint(`unsafe`)\nPY", "stdout": "private"})
    assert "\n" not in summary
    assert summary.count("`") == 2
    assert "private" not in summary


def test_native_identity_switches_binding_and_keeps_per_thread_cursors(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    info.pop("agent_session")
    source.bind(info, "session-1")
    info["agent_status"] = "idle"
    source.sync(info, lambda *args: None)
    new = tmp_path / "new.jsonl"
    write_rows(new, [row("session_meta", {"id": "session-2", "cwd": "/project"})])
    state.execute("INSERT INTO threads VALUES ('session-2', ?, '/project', 0, NULL)", (str(new),))
    state.commit()
    changed = {**info, "agent_session": {"value": "session-2"}}
    source.sync(changed, lambda *args: None)
    assert source.bound_info(changed)["agent_session"]["value"] == "session-2"
    assert db.execute("SELECT thread_id FROM codex_session_bindings").fetchone()[0] == "session-2"
    write_rows(new, [row("event_msg", {"type": "user_message", "message": "new conversation"})])
    write_rows(rollout, [row("event_msg", {"type": "user_message", "message": "old conversation"})])
    enqueue = lambda event_id, agent, message: db.execute("INSERT INTO outbox VALUES (?, ?)", (event_id, json.dumps(message)))
    source.sync(changed, enqueue)
    source.sync(changed, enqueue)
    assert [m["text"] for _, m in collect(db)] == ["new conversation"]
    # A resume back to the old exact thread continues its saved cursor.
    source.sync({**info, "agent_session": {"value": "session-1"}}, enqueue)
    assert [m["text"] for _, m in collect(db)] == ["new conversation", "old conversation"]
    assert db.execute("SELECT count(*) FROM codex_sync_cursors").fetchone()[0] == 2


def test_invalid_native_switch_keeps_original_binding_and_cursor(tmp_path):
    source, db, state, _, info = setup_source(tmp_path, [])
    source.sync(info, lambda *args: None)
    before = db.execute("SELECT * FROM codex_sync_cursors").fetchall()
    source.sync({**info, "agent_session": {"value": "missing"}}, lambda *args: pytest.fail("wrong session"))
    assert db.execute("SELECT thread_id FROM codex_session_bindings").fetchone()[0] == "session-1"
    assert db.execute("SELECT * FROM codex_sync_cursors").fetchall() == before


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, RuntimeError])
def test_enqueue_failure_rolls_back_seen_outbox_and_cursor_then_recovers(tmp_path, failure):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    source.sync(info, lambda *args: None)
    baseline = db.execute("SELECT offset FROM codex_sync_cursors").fetchone()[0]
    write_rows(rollout, [row("event_msg", {"type": "user_message", "message": "recover me"})])
    def broken(event_id, agent, message):
        db.execute("INSERT INTO outbox VALUES (?,?)", (event_id, json.dumps(message)))
        raise failure("injected failure after enqueue")
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            source.sync(info, broken)
    else:
        source.sync(info, broken)
    assert collect(db) == []
    assert db.execute("SELECT count(*) FROM codex_sync_seen").fetchone()[0] == 0
    assert db.execute("SELECT offset FROM codex_sync_cursors").fetchone()[0] == baseline
    enqueue = lambda event_id, agent, message: db.execute("INSERT INTO outbox VALUES (?,?)", (event_id, json.dumps(message)))
    source.sync(info, enqueue)
    source.sync(info, enqueue)
    assert [m["text"] for _, m in collect(db)] == ["recover me"]


def test_closed_database_restart_preserves_pending_and_acked_messages(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    source.sync(info, lambda *args: None)
    write_rows(rollout, [row("event_msg", {"type": "user_message", "message": "pending during outage"})])
    enqueue = lambda event_id, agent, message: db.execute("INSERT INTO outbox VALUES (?,?)", (event_id, json.dumps(message)))
    source.sync(info, enqueue)
    original = collect(db)
    db.commit()
    db.close()
    db = sqlite3.connect(tmp_path / "bridge.sqlite")
    source = CodexRolloutSync(db, home=source.home)
    source.sync(info, lambda *args: pytest.fail("restart must not enqueue duplicates"))
    assert collect(db) == original
    db.execute("DELETE FROM outbox")
    db.commit()
    db.close()
    db = sqlite3.connect(tmp_path / "bridge.sqlite")
    source = CodexRolloutSync(db, home=source.home)
    source.sync(info, lambda *args: pytest.fail("acked message must not replay"))
    assert collect(db) == []


def test_resume_without_new_answer_cannot_duplicate_completion_notifications(tmp_path):
    source, db, state, rollout, info = setup_source(tmp_path, [])
    source.sync(info, lambda *args: None)
    assert not source.claim_completion(info)
    write_rows(rollout, [row("event_msg", {"type": "item_completed", "item": {
        "type": "AgentMessage", "id": "answer-1", "phase": "final_answer", "text": "finished"}})])
    source.sync(info, lambda *args: None)
    assert source.claim_completion(info)
    assert not source.claim_completion(info)
    db.commit()
    restarted = CodexRolloutSync(db, home=source.home)
    restarted.sync(info, lambda *args: pytest.fail("old answer replayed"))
    assert not restarted.claim_completion(info)
    write_rows(rollout, [row("event_msg", {"type": "item_completed", "item": {
        "type": "AgentMessage", "id": "answer-2", "phase": "final_answer", "text": "next task"}})])
    restarted.sync(info, lambda *args: None)
    assert restarted.claim_completion(info)
