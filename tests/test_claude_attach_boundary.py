import sqlite3

from herdr_bridge.claude_sync import ClaudeTranscriptSync
from test_claude_review_regressions import TID, append, answer, user


def test_first_active_scan_keeps_answer_before_queued_prompt(tmp_path):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    queued = {"type": "attachment", "uuid": "queued", "sessionId": TID,
              "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "queued question"}}
    append(path, [user("history", "old"), answer("history answer", "old-a"),
                  user("first question", "u1"), answer("first answer", "a1"), queued,
                  answer("second answer", "a2"), {"type": "system", "subtype": "turn_duration"}])
    db = sqlite3.connect(":memory:")
    sync = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "working", "agent_session": {"value": TID}}
    emitted = []
    try:
        sync.sync(info, lambda eid, agent, message: emitted.append(message))
        assert [(m["role"], m["text"]) for m in emitted] == [
            ("user", "first question"), ("assistant", "first answer"),
            ("user", "queued question"), ("assistant", "second answer")]
    finally:
        db.close()


def test_completed_history_retires_typed_anchor_before_new_event(tmp_path):
    path = tmp_path / "transcript.jsonl"
    append(path, [user("historical question", "old"), answer("historical answer", "old-a"),
                  {"type": "system", "subtype": "turn_duration"}])
    event_offset = path.stat().st_size
    append(path, [user("private summary", "compact", isCompactSummary=True)])
    db = sqlite3.connect(":memory:")
    try:
        sync = ClaudeTranscriptSync(db, home=tmp_path)
        assert sync.latest_turn_offset(path) == event_offset
    finally:
        db.close()
