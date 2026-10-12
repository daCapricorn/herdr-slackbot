import json
import sqlite3
from pathlib import Path
import pytest

from herdr_bridge.claude_sync import ClaudeTranscriptSync, resolve_claude
from herdr_bridge.codex_session import SessionUnavailable
from herdr_bridge.agent_adapters import foreground_claude_session

TID = "3fbc438b-2720-4718-8eb2-a9fdf402ebfd"
NEW = "c6565ec4-87cd-4855-b9ee-4a940f248743"


def append(path, rows):
    with path.open("ab") as file:
        for row in rows:
            file.write(json.dumps(row).encode() + b"\n")


def user(text="prompt", uid="u1"):
    return {"type": "user", "uuid": uid, "sessionId": TID, "cwd": "/project", "message": {"content": text}}


def answer(text="answer", mid="m1", uid="a1", stop="end_turn"):
    return {"type": "assistant", "uuid": uid, "sessionId": TID, "cwd": "/project", "message": {
        "id": mid, "stop_reason": stop, "content": [{"type": "text", "text": text}]}}


def end():
    return {"type": "system", "subtype": "turn_duration", "sessionId": TID, "cwd": "/project"}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "projects/-project" / (TID + ".jsonl")
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old"), answer("old", "old", "old-a"), end()])
    db = sqlite3.connect(tmp_path / "bridge.sqlite")
    db.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY, event TEXT)")
    sync = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project", "agent_status": "idle", "agent_session": {"value": TID}}
    def enqueue(eid, agent, message):
        db.execute("INSERT INTO outbox VALUES (?,?)", (eid, json.dumps(message)))
    yield sync, db, path, info, enqueue
    db.close()


def messages(db):
    return [json.loads(r[0]) for r in db.execute("SELECT event FROM outbox ORDER BY rowid")]


def test_baseline_skips_history_and_streams_user_final_and_tool_summary(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    assert not messages(db) and not sync.claim_completion(info)
    append(path, [user(), {**answer(), "message": {"id": "tool-msg", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "private thoughts"},
        {"type": "text", "text": "working commentary"},
        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "TOKEN=privatevalue uname -s"}}]}},
        user([{"type": "tool_result", "content": "private output"}], "result"), answer(), end()])
    sync.sync({**info, "agent_status": "working"}, enqueue)
    assert messages(db) == [{"role": "user", "text": "prompt"}, {"role": "tool", "text": "Ran: `TOKEN=[redacted] uname -s`"}, {"role": "assistant", "text": "answer"}]
    assert sync.claim_completion(info) and not sync.claim_completion(info)
    sync.sync(info, lambda *args: pytest.fail("duplicate"))


def test_split_answer_stages_across_restart_and_waits_for_turn_end(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    append(path, [user(), {**answer(), "message": {"id": "m1", "stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "secret"}]}}])
    sync.sync({**info, "agent_status": "working"}, enqueue)
    append(path, [answer("part one"), answer("part two", uid="a2")])
    db.commit()
    reopened = sqlite3.connect(db.execute("PRAGMA database_list").fetchone()[2])
    other = ClaudeTranscriptSync(reopened, home=sync.home)
    other_enqueue = lambda eid, agent, message: reopened.execute("INSERT INTO outbox VALUES (?,?)", (eid, json.dumps(message)))
    other.sync({**info, "agent_status": "working"}, other_enqueue)
    assert len(messages(reopened)) == 1
    append(path, [end()])
    other.sync(info, other_enqueue)
    assert messages(reopened)[-1] == {"role": "assistant", "text": "part one\npart two"}
    reopened.commit()
    reopened.close()


def test_partial_final_line_is_not_consumed_or_flushed(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    append(path, [user(), answer("one")])
    last = json.dumps(answer("two", uid="a2")).encode()
    with path.open("ab") as file:
        file.write(last[:20])
    sync.sync(info, enqueue)
    assert messages(db) == [{"role": "user", "text": "prompt"}]
    with path.open("ab") as file:
        file.write(last[20:] + b"\n")
    append(path, [end()])
    sync.sync(info, enqueue)
    assert messages(db)[-1]["text"] == "one\ntwo"


def test_failed_enqueue_rolls_back_pending_cursor_and_seen(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    old = db.execute("SELECT offset FROM claude_sync_cursors").fetchone()[0]
    append(path, [user(), answer(), end()])
    def failed(eid, agent, message):
        enqueue(eid, agent, message)
        if message["role"] == "assistant":
            raise OSError("offline")
    sync.sync(info, failed)
    assert messages(db) == [] and not db.execute("SELECT * FROM claude_sync_pending").fetchall()
    assert db.execute("SELECT offset FROM claude_sync_cursors").fetchone()[0] == old
    sync.sync(info, enqueue)
    assert len(messages(db)) == 2


def test_exact_session_switch_retains_separate_cursors_and_no_old_completion(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    other_path = path.with_name(NEW + ".jsonl")
    append(other_path, [{**user("previous", "new-old"), "sessionId": NEW}])
    other_info = {**info, "agent_session": {"value": NEW}}
    sync.sync(other_info, enqueue)
    assert not sync.claim_completion(other_info)
    append(other_path, [{**user("new session"), "sessionId": NEW}, {**answer(), "sessionId": NEW}, {**end(), "sessionId": NEW}])
    sync.sync(other_info, enqueue)
    sync.sync(info, enqueue)
    assert db.execute("SELECT count(*) FROM claude_sync_cursors").fetchone()[0] == 2
    assert db.execute("SELECT thread_id FROM claude_session_bindings").fetchone()[0] == TID
    assert [m["text"] for m in messages(db)] == ["new session", "answer"]


def test_subagent_meta_and_local_commands_never_emit(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    append(path, [{**user("nested"), "isSidechain": True}, {**user("meta"), "isMeta": True}, user("<command-name>/status</command-name>"), user("[Request interrupted by user]"), {**answer("nested"), "isSidechain": True}, end()])
    sync.sync(info, enqueue)
    assert messages(db) == []


def test_wrong_transcript_identity_and_no_id_fail_closed(source):
    sync, db, path, info, enqueue = source
    with pytest.raises(SessionUnavailable):
        resolve_claude({**info, "agent_session": None}, home=sync.home)
    with pytest.raises(SessionUnavailable):
        resolve_claude({**info, "cwd": "/wrong"}, home=sync.home)
    with pytest.raises(SessionUnavailable):
        sync.bind(info, NEW)
    sync.sync(info, enqueue)
    append(path, [{**user("wrong"), "sessionId": NEW}])
    sync.sync(info, enqueue)
    assert messages(db) == [] and sync.diagnostic(info)["state"] == "unavailable"


def test_pid_registry_checks_reuse_directory_kind_and_ambiguity(tmp_path):
    sessions = tmp_path / "sessions"; sessions.mkdir()
    record = {"pid": 123, "procStart": "actual start", "cwd": "/project", "kind": "interactive", "sessionId": TID}
    path = sessions / "123.json"
    path.write_text(json.dumps(record))
    info = {"agent": "claude", "pane_id": "w1:p1", "cwd": "/project"}
    process = {"pane_id": "w1:p1", "foreground_processes": [{"pid": 123}]}
    resolve = lambda: foreground_claude_session(info, process, home=tmp_path, started=lambda _: "actual start")
    assert resolve() == TID
    for key, value in [("procStart", "old start"), ("cwd", "/wrong"), ("kind", "subagent"), ("sessionId", "../../invalid")]:
        path.write_text(json.dumps({**record, key: value})); assert resolve() is None
    path.write_text(json.dumps(record))
    (sessions / "124.json").write_text(json.dumps({**record, "pid": 124}))
    process["foreground_processes"].append({"pid": 124})
    assert resolve() is None


def test_idle_without_durable_turn_boundary_does_not_commit_early(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    append(path, [user(), answer('first block')])
    sync.sync(info, enqueue)
    assert messages(db) == [{'role':'user','text':'prompt'}]
    append(path, [answer('late block', uid='late'), end()])
    sync.sync(info, enqueue)
    assert messages(db)[-1]['text'] == 'first block\nlate block'


def test_malformed_message_shapes_are_skipped_and_replaced_file_discards_staged_text(source):
    sync, db, path, info, enqueue = source
    sync.sync(info, enqueue)
    append(path, [{'type':'user','message':'invalid'}, {'type':'assistant','message':[]}, user(), answer('discard stale')])
    sync.sync({**info,'agent_status':'working'}, enqueue)
    replacement = path.with_suffix('.tmp')
    append(replacement, [user('replacement','replacement'), answer('fresh','fresh','fresh'),end()])
    replacement.replace(path)
    sync.sync({**info,'agent_status':'working'}, enqueue)
    assert 'discard stale' not in json.dumps(messages(db))
    assert messages(db)[-1]['text'] == 'fresh'
