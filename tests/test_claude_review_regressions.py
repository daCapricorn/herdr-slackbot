import json
import sqlite3

import pytest

from herdr_bridge.claude_sync import ClaudeTranscriptSync


TID = "3fbc438b-2720-4718-8eb2-a9fdf402ebfd"


def user(text, uid, **fields):
    return {"type": "user", "uuid": uid, "sessionId": TID, "cwd": "/project",
            "message": {"content": text}, **fields}


def answer(text, uid):
    return {"type": "assistant", "uuid": uid, "sessionId": TID, "cwd": "/project",
            "message": {"id": uid, "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]}}


def append(path, rows):
    with path.open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    db = sqlite3.connect(":memory:")
    sync = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a",
            "cwd": "/project", "agent_status": "idle", "agent_session": {"value": TID}}
    emitted = []
    enqueue = lambda eid, agent, message: emitted.append(message)
    sync.sync(info, enqueue)
    yield sync, path, info, enqueue, emitted
    db.close()


@pytest.mark.parametrize("record", [
    user("<task-notification><summary>done</summary></task-notification>", "system", promptSource="system"),
    user("injected note", "system", promptSource="system"),
    user("private compact summary", "compact", isCompactSummary=True),
    user("private compact summary", "compact", isVisibleInTranscriptOnly=True),
    user("<bash-stdout>private output</bash-stdout>", "bash"),
])
def test_non_owner_record_never_emits_as_user(source, record):
    sync, path, info, enqueue, emitted = source
    append(path, [user("real question", "u1"), answer("first answer", "a1"), record,
                  {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [m["text"] for m in emitted if m["role"] == "user"] == ["real question"]
    assert [m["text"] for m in emitted if m["role"] == "assistant"] == ["first answer"]


def test_queued_prompt_preserves_previous_answer(source):
    sync, path, info, enqueue, emitted = source
    queued = {"type": "attachment", "uuid": "queued", "sessionId": TID,
              "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "queued question"}}
    append(path, [user("first question", "u1"), answer("first answer", "a1"), queued,
                  answer("second answer", "a2"), {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [
        ("user", "first question"), ("assistant", "first answer"),
        ("user", "queued question"), ("assistant", "second answer")]
    sync.sync(info, lambda *args: pytest.fail("duplicate event"))


@pytest.mark.parametrize("scheme", ["Bearer", "Basic"])
def test_bash_authorization_is_redacted_in_outgoing_event(source, scheme):
    sync, path, info, enqueue, emitted = source
    credential = "SYNTHETIC_CREDENTIAL"
    tool = {"type": "assistant", "uuid": "tool", "sessionId": TID,
            "message": {"id": "tool", "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "bash1", "name": "Bash", "input": {
                    "command": f'curl -H "Authorization: {scheme} {credential}" https://example.invalid'}}]}}
    append(path, [user("check", "u1"), tool])
    sync.sync(info, enqueue)
    summary = next(m["text"] for m in emitted if m["role"] == "tool")
    assert credential not in summary
    assert f"Authorization: {scheme} [redacted]" in summary
