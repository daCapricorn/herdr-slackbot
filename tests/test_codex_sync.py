import json
import os
import sqlite3

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
    write_rows(rollout, rows)
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
            "type": "AgentMessage", "id": "answer-1", "phase": "final_answer", "text": "Fixed it."}}, ordinal=6),
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
    write_rows(replacement, rows + [row("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u2",
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
