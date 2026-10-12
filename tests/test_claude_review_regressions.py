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


def queued(prompt, uid, mode="prompt"):
    return {"type": "attachment", "uuid": uid, "sessionId": TID,
            "attachment": {"type": "queued_command", "commandMode": mode, "prompt": prompt}}


def test_queued_image_prompt_and_other_modes(source):
    sync, path, info, enqueue, emitted = source
    blocks = [{"type": "image", "source": {}}, {"type": "text", "text": "look"}]
    append(path, [user("first question", "u1"), answer("first answer", "a1"),
                  queued("private notification", "q0", mode="task-notification"),
                  queued(blocks, "q1"), answer("second answer", "a2"), {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [
        ("user", "first question"), ("assistant", "first answer"),
        ("user", "[image]\n\nlook"), ("assistant", "second answer")]


def test_malformed_text_is_skipped_and_later_rows_still_sync(source):
    sync, path, info, enqueue, emitted = source
    null_text = [{"type": "text", "text": None}]
    append(path, [user(null_text, "bad"), queued(null_text, "bad-q"), user("real question", "u1"),
                  answer("real answer", "a1"), {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [("user", "real question"), ("assistant", "real answer")]
    assert sync.diagnostic(info)["offset"] == path.stat().st_size


def test_question_reply_is_not_emitted_as_user(source):
    sync, path, info, enqueue, emitted = source
    append(path, [user("first question", "u1"),
                  user("<send_user_message_question_reply>yes</send_user_message_question_reply>", "r1"),
                  answer("final", "a1"), {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [("user", "first question"), ("assistant", "final")]


def test_latest_turn_offset_uses_shared_classifier(source):
    sync, path, *_ = source
    append(path, [answer("old answer", "a0"), {"type": "system", "subtype": "turn_duration"}])
    queued_at = path.stat().st_size
    append(path, [queued("queued question", "q1"), user("<bash-stdout>out</bash-stdout>", "bash")])
    assert sync.latest_turn_offset(path) == queued_at
    compact_at = path.stat().st_size
    append(path, [user("private compact summary", "compact", isCompactSummary=True),
                  user([{"type": "text", "text": None}], "bad")])
    assert sync.latest_turn_offset(path) == compact_at


@pytest.mark.parametrize("prompt", [[{"type": "text", "text": ""}],
                                  [{"type": "text", "text": "  "}],
                                  "<system-reminder>private note</system-reminder>"])
def test_empty_cleaned_queued_prompt_is_not_a_boundary(source, prompt):
    from herdr_bridge.claude_records import turn_start
    sync, path, info, enqueue, emitted = source
    record = queued(prompt, "q1")
    assert turn_start(record) is None
    append(path, [user("question", "u1"), answer("final", "a1"), record,
                  {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [("user", "question"), ("assistant", "final")]


def test_new_assistant_message_preserves_terminal_answer_across_ignored_bash(source):
    sync, path, info, enqueue, emitted = source
    append(path, [queued("question", "q1"), answer("first final", "a1"),
                  user("<bash-stdout>private output</bash-stdout>", "bash"),
                  answer("second final", "a2"), {"type": "system", "subtype": "turn_duration"}])
    sync.sync(info, enqueue)
    assert [(m["role"], m["text"]) for m in emitted] == [
        ("user", "question"), ("assistant", "first final"), ("assistant", "second final")]


@pytest.mark.parametrize("flags", [{"isMeta": True}, {"isSidechain": True}])
def test_shared_classifier_rejects_queued_meta_and_sidechain(flags):
    from herdr_bridge.claude_records import turn_start
    assert turn_start({**queued("private", "q1"), **flags}) is None


def test_shared_classifier_rejects_question_reply():
    from herdr_bridge.claude_records import turn_start
    assert turn_start(user("<send_user_message_question_reply>yes</send_user_message_question_reply>", "r1")) is None


@pytest.mark.parametrize("command,expected", [
    ('curl -H "Authorization: Bearer abc.def"', 'curl -H "Authorization: Bearer [redacted]"'),
    ("curl -H 'authorization:basic   dXNlcjpwdw=='", "curl -H 'authorization:basic   [redacted]'"),
    ('curl -H "Authorization: Bearer \'abc\'"', 'curl -H "Authorization: Bearer \'[redacted]\'"'),
    ("Authorization=Bearer abc, next", "Authorization=Bearer [redacted], next"),
    ("TOKEN=abc x--token=def", "TOKEN=[redacted] x--token=[redacted]"),
    ("tool --token abc --password=def", "tool --token [redacted] --password=[redacted]"),
    ('tool --api-key "secret with spaces"', 'tool --api-key "[redacted]"'),
    ("tool --password 'secret with spaces'", "tool --password '[redacted]'"),
    ('TOKEN="secret with spaces" tool', 'TOKEN="[redacted]" tool'),
    ("PASSWORD='secret with spaces' tool", "PASSWORD='[redacted]' tool"),
])
def test_redact_authorization_variants(command, expected):
    from herdr_bridge.codex_sync import _redact
    assert _redact(command) == expected


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
