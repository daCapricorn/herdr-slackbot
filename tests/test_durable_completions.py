"""Native-shaped completion association regressions; no completion gate stubs."""
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_bridge.core import Bridge
from herdr_bridge.codex_sync import CodexRolloutSync
from test_codex_sync import row, write_rows
from test_claude_sync import TID as CLAUDE_TID, append, user, answer, end

NATIVE = [json.loads(line) for line in (Path(__file__).parent / "fixtures/transcripts/codex_completion_native.jsonl").read_text().splitlines()]
TID = NATIVE[1]["payload"]["thread_id"]


class Client:
    def __init__(self):
        self.info = {"agent": "codex", "pane_id": "w1:p1", "terminal_id": "terminal-1", "cwd": "/project",
                     "agent_session": {"value": TID}, "agent_status": "idle", "name": "answer owner"}
    def get_agent(self, pane): return copy.deepcopy(self.info)
    def list_agents(self): return [self.get_agent("w1:p1")]
    def list_tabs(self, *args): return []
    def process_info(self, pane):
        return {"pane_id": pane, "foreground_processes": [{"pid": 100, "argv": ["codex"]}]}
    def read_agent_once(self, *args, **kwargs):
        pytest.fail("completion must never read the partial B viewport")


@pytest.fixture
def native_bridge(tmp_path):
    client = Client()
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    home = tmp_path / "codex"; home.mkdir()
    path = tmp_path / "rollout.jsonl"
    write_rows(path, [row("session_meta", {"id": TID, "cwd": "/project"})])
    state = sqlite3.connect(home / "state_1.sqlite")
    state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, agent_path TEXT)")
    state.execute("INSERT INTO threads VALUES (?, ?, '/project', 0, NULL)", (TID, str(path)))
    state.commit()
    bridge.adapters["codex"].source.home = home
    bridge.sync_conversation(client.info)  # exact first-attach cutoff, no old history
    yield bridge, client, path, state
    bridge.close(); state.close()


def events(bridge, kind="agent.done"):
    return [json.loads(raw) for (raw,) in bridge.db.execute("SELECT event FROM outbox ORDER BY rowid")
            if json.loads(raw)["type"] == kind]


def test_old_answer_notification_uses_A_while_B_viewport_is_partial(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:10])  # A closed, B has only begun
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    assert events(bridge)[0]["summary"] == "HERDR_FIFO_RETRY_A_20261013_OK"
    assert events(bridge)[0]["completion"]["turn_id"] == NATIVE[4]["payload"]["turn_id"]
    write_rows(path, NATIVE[10:11])  # B final item precedes task_complete
    client.info["agent_status"] = "idle"
    bridge.transition(SimpleNamespace(ended=False, info=client.info, status="idle", prev_status="working"))
    assert len(events(bridge)) == 1
    assert [e["text"] for e in events(bridge, "conversation.message") if e["role"] == "assistant"] == [
        "HERDR_FIFO_RETRY_A_20261013_OK", "HERDR_FIFO_RETRY_B_20261013_OK"]
    write_rows(path, NATIVE[11:])
    client.info["agent_status"] = "working"  # next turn already active
    bridge.discover(client.info)
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK", "HERDR_FIFO_RETRY_B_20261013_OK"]


@pytest.mark.parametrize("count", [2, 3])
def test_multiple_native_answers_in_one_scan_are_queued_in_order(native_bridge, count):
    bridge, client, path, _ = native_bridge
    rows = NATIVE[-count * 4:]
    write_rows(path, rows)
    client.info["agent_status"] = "working"
    bridge.sync_conversation(client.info)
    bridge.discover(client.info)
    expected = [r["payload"]["last_agent_message"] for r in rows if r["payload"]["type"] == "task_complete"]
    assert [e["text"] for e in events(bridge)] == expected
    assert [e["text"] for e in events(bridge, "conversation.message") if e["role"] == "assistant"] == expected


def test_partial_closure_and_wrong_turn_do_not_complete(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7])
    write_rows(path, [row("event_msg", {"type": "task_complete", "turn_id": "unrelated"})])
    bridge.discover(client.info)
    assert events(bridge) == []
    closure = json.dumps(NATIVE[7]).encode()
    with path.open("ab") as stream: stream.write(closure[:100])
    bridge.discover(client.info)
    assert events(bridge) == []
    with path.open("ab") as stream: stream.write(closure[100:] + b"\n")
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]


def test_final_staging_survives_restart_and_unavailable_source(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7])
    bridge.sync_conversation(client.info)
    assert events(bridge) == []
    dbpath = bridge.db.execute("PRAGMA database_list").fetchone()[2]
    home = bridge.adapters["codex"].source.home
    bridge.close()
    bridge.__init__(client, dbpath)
    bridge.adapters["codex"].source.home = home
    write_rows(path, NATIVE[7:8])
    saved = path.with_suffix(".saved"); path.rename(saved)
    bridge.discover(client.info)
    assert events(bridge) == []
    saved.rename(path)
    client.info["name"] = "later display name"
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert events(bridge)[0]["agent"]["name"] == "answer owner"
    assert len(events(bridge)) == 1
    bridge.db.execute("DELETE FROM outbox"); bridge.db.commit()
    bridge.discover(client.info)
    assert events(bridge) == []


def test_atomic_outbox_failure_retries_every_completion_once(native_bridge, monkeypatch):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE)
    queue = bridge._queue_completion
    delivered = []
    def fail_second(event_id, record):
        queue(event_id, record)
        delivered.append(event_id)
        if len(delivered) == 2: raise RuntimeError("outbox failure")
    monkeypatch.setattr(bridge, "_queue_completion", fail_second)
    bridge.sync_conversation(client.info)
    assert events(bridge) == []
    assert not bridge.db.execute("SELECT * FROM codex_completion_seen").fetchall()
    monkeypatch.setattr(bridge, "_queue_completion", queue)
    bridge.sync_conversation(client.info)
    assert len(events(bridge)) == 3
    bridge.sync_conversation(client.info)
    assert len(events(bridge)) == 3


def test_session_snapshot_survives_new_session_without_re_enrichment(native_bridge, monkeypatch):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters["codex"].source
    write_rows(path, NATIVE[4:8])
    agent = {"pane_id": "w1:p1", "terminal_id": "terminal-1", "session": TID, "kind": "codex", "name": "original"}
    source.sync(client.info, bridge._queue_conversation, completion_agent=agent)
    client.info["agent_session"] = {"value": "new-session"}
    # A durable closed record does not resolve or re-enrich during its drain.
    monkeypatch.setattr(source, "_resolve", lambda *args: pytest.fail("re-resolved"))
    monkeypatch.setattr(bridge, "_enrich_agent", lambda *args: pytest.fail("re-enriched"))
    with bridge.db:
        assert source.drain_completions(client.info, bridge._queue_completion) == 1
    assert events(bridge)[0]["agent"] == agent
    assert events(bridge)[0]["completion"]["session_id"] == TID


@pytest.mark.parametrize("already_seen", [False, True])
def test_legacy_final_migration_never_replays_history(native_bridge, already_seen):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters["codex"].source
    bridge.db.execute("INSERT INTO codex_sync_final VALUES (?,?)", (f"terminal-1:{TID}", "old-final"))
    if already_seen: bridge.db.execute("INSERT INTO codex_completion_seen VALUES ('old-final')")
    bridge.db.commit()
    source = CodexRolloutSync(bridge.db, home=source.home)
    bridge.adapters["codex"].source = source
    bridge.discover(client.info)
    assert events(bridge) == []
    write_rows(path, NATIVE[4:8])
    bridge.discover(client.info)
    assert len(events(bridge)) == 1


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_untracked_supported_startup_never_uses_viewport(tmp_path, kind):
    client = Client(); client.info["agent"] = kind
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    try:
        bridge.transition(SimpleNamespace(ended=False, info=client.info, status="idle", prev_status="working"))
        bridge.discover(client.info)
        assert events(bridge) == []
    finally: bridge.close()


def test_claude_multiple_answers_reuse_native_staging_and_original_metadata(tmp_path):
    client = Client(); client.info.update(agent="claude", agent_session={"value": CLAUDE_TID})
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    path = tmp_path / "claude/projects/-project" / (CLAUDE_TID + ".jsonl"); path.parent.mkdir(parents=True)
    append(path, [user("old", uid="old"), answer("old", mid="old"), end()])
    bridge.adapters["claude"].source.home = tmp_path / "claude"
    try:
        bridge.sync_conversation(client.info)
        append(path, [user("one", uid="u1"), answer("A", mid="a1"), end(),
                      user("two", uid="u2"), answer("B", mid="a2"), end()])
        client.info["agent_status"] = "working"
        bridge.discover(client.info)
        assert [e["text"] for e in events(bridge)] == ["A", "B"]
        bridge.discover(client.info)
        assert [e["text"] for e in events(bridge)] == ["A", "B"]
    finally: bridge.close()


def test_legacy_sync_seen_answer_never_restages_on_cursor_file_reset(native_bridge):
    from herdr_bridge.codex_sync import _stable_id
    bridge, client, path, _ = native_bridge
    event_id = _stable_id(f"terminal-1:{TID}", NATIVE[6], 0, "assistant", "HERDR_FIFO_RETRY_A_20261013_OK")
    bridge.db.execute("INSERT INTO codex_sync_seen VALUES (?)", (event_id,)); bridge.db.commit()
    replacement = path.with_suffix(".replacement")
    write_rows(replacement, [row("session_meta", {"id": TID, "cwd": "/project"})] + NATIVE[4:8])
    replacement.replace(path)
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert events(bridge) == []
    assert not bridge.db.execute("SELECT * FROM codex_completion_pending").fetchall()
    assert not bridge.db.execute("SELECT * FROM codex_completions").fetchall()
    write_rows(path, NATIVE[8:])
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_B_20261013_OK"]


def test_durable_queue_and_independent_drain_rollback_survive_restart(native_bridge):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters["codex"].source
    write_rows(path, NATIVE[4:])
    agent = {"pane_id": "w1:p1", "terminal_id": "terminal-1", "session": TID, "kind": "codex"}
    source.sync(client.info, bridge._queue_conversation, completion_agent=agent)
    bridge.db.commit()
    assert len(bridge.db.execute("SELECT * FROM codex_completions").fetchall()) == 2
    def fail(event_id, record):
        bridge._queue_completion(event_id, record)
        raise RuntimeError("after durable queue, before claim")
    with pytest.raises(RuntimeError): source.drain_completions(client.info, fail)
    assert events(bridge) == []
    assert not bridge.db.execute("SELECT * FROM codex_completion_seen").fetchall()
    dbpath = bridge.db.execute("PRAGMA database_list").fetchone()[2]
    home = source.home
    bridge.close(); bridge.__init__(client, dbpath)
    bridge.adapters["codex"].source.home = home
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK", "HERDR_FIFO_RETRY_B_20261013_OK"]
    bridge.discover(client.info)
    assert len(events(bridge)) == 2


def test_legacy_seen_exact_answer_id_suppresses_only_its_completion(native_bridge):
    from herdr_bridge.codex_sync import _stable_id
    bridge, client, path, _ = native_bridge
    event_id = _stable_id(f"terminal-1:{TID}", NATIVE[6], 0, "assistant", "HERDR_FIFO_RETRY_A_20261013_OK")
    bridge.db.execute("INSERT INTO codex_completion_seen VALUES (?)", (event_id,)); bridge.db.commit()
    write_rows(path, NATIVE[4:])
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_B_20261013_OK"]
    assert [e["text"] for e in events(bridge, "conversation.message") if e["role"] == "assistant"] == [
        "HERDR_FIFO_RETRY_A_20261013_OK", "HERDR_FIFO_RETRY_B_20261013_OK"]


def test_native_final_item_before_closure_is_conversation_only(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7])
    client.info["agent_status"] = "idle"
    bridge.discover(client.info)
    assert events(bridge) == []
    assert [e["text"] for e in events(bridge, "conversation.message") if e["role"] == "assistant"] == [
        "HERDR_FIFO_RETRY_A_20261013_OK"]
    write_rows(path, NATIVE[7:8])
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]


def test_late_A_closure_after_B_start_drains_A_without_waiting_for_B(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7] + NATIVE[8:10])
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert events(bridge) == []
    write_rows(path, NATIVE[7:8])
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    write_rows(path, NATIVE[10:])
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK", "HERDR_FIFO_RETRY_B_20261013_OK"]


def test_restart_closed_A_delivers_while_new_B_transcript_is_unavailable(native_bridge):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters["codex"].source
    write_rows(path, NATIVE[4:8])
    snapshot = {"pane_id": "w1:p1", "terminal_id": "terminal-1", "session": TID,
                "kind": "codex", "name": "A snapshot"}
    # Persist ready A, then crash before Core can drain it into the outbox.
    source.sync(client.info, bridge._queue_conversation, completion_agent=snapshot)
    bridge.db.commit()
    dbpath = bridge.db.execute("PRAGMA database_list").fetchone()[2]
    home = source.home
    bridge.close(); bridge.__init__(client, dbpath)
    bridge.adapters["codex"].source.home = home
    client.info.update(agent_session={"value": "new-B-session"}, agent_status="working", name="B current")
    bridge.discover(client.info)
    assert bridge.adapters["codex"].source.diagnostic(client.info)["state"] == "unavailable"
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    assert events(bridge)[0]["agent"] == snapshot
    bridge.discover(client.info)
    assert len(events(bridge)) == 1


@pytest.mark.parametrize("status,later_B", [("working", False), ("idle", False), ("working", True), ("idle", True)])
def test_registered_answer_revalidates_after_rotation_before_later_turn_or_EOF(native_bridge, status, later_B):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    original = bridge.db.execute("SELECT record FROM codex_completion_pending").fetchone()[0]
    replacement = path.with_suffix(".rotated")
    rows = NATIVE[4:8] + (NATIVE[8:10] if later_B else [])
    write_rows(replacement, [row("session_meta", {"id": TID, "cwd": "/project"})] + rows)
    replacement.replace(path)
    client.info.update(agent_status=status, name="replacement display")
    bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    completion = json.loads(bridge.db.execute("SELECT record FROM codex_completions").fetchone()[0])
    captured = json.loads(original)
    for key in ("agent", "source", "info", "text", "event_id"):
        assert completion[key] == captured[key]
    assert [e["text"] for e in events(bridge, "conversation.message") if e["role"] == "assistant"] == [
        "HERDR_FIFO_RETRY_A_20261013_OK"]


@pytest.mark.parametrize("canonical", ["foreign final body", "", None])
def test_explicit_canonical_closure_must_match_a_registered_answer(native_bridge, canonical):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    closure = copy.deepcopy(NATIVE[7]); closure["payload"]["last_agent_message"] = canonical
    write_rows(path, [closure]); bridge.discover(client.info)
    assert events(bridge) == []
    assert bridge.db.execute("SELECT count(*) FROM codex_completion_pending").fetchone()[0] == 1
    write_rows(path, NATIVE[7:8]); bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]


def test_canonical_closure_selects_final_without_partial_phase_absent_duplicate(native_bridge):
    bridge, client, path, _ = native_bridge
    partial = copy.deepcopy(NATIVE[6])
    partial["payload"]["item"].pop("phase")
    partial["payload"]["item"].update(id="partial-id", content=[{"type": "Text", "text": "partial body"}])
    write_rows(path, NATIVE[4:6] + [partial] + NATIVE[6:8]); bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    assert not bridge.db.execute("SELECT * FROM codex_completion_pending").fetchall()


@pytest.mark.parametrize("phase,partial,expected", [("final_answer", False, True), (None, False, False),
                                                   ("final_answer", True, False)])
def test_closure_without_canonical_text_requires_one_explicit_final(native_bridge, phase, partial, expected):
    bridge, client, path, _ = native_bridge
    final = copy.deepcopy(NATIVE[6]); final["payload"]["item"]["phase"] = phase
    rows = NATIVE[4:6]
    if partial:
        earlier = copy.deepcopy(NATIVE[6])
        earlier["payload"]["item"].pop("phase")
        earlier["payload"]["item"].update(id="earlier-id", content=[{"type": "Text", "text": "earlier"}])
        rows += [earlier]
    closure = copy.deepcopy(NATIVE[7]); closure["payload"].pop("last_agent_message")
    write_rows(path, rows + [final, closure]); bridge.discover(client.info)
    assert len(events(bridge)) == int(expected)


@pytest.mark.parametrize("rewrite", ["replace", "truncate"])
def test_conflicting_rotated_answer_holds_original_registration(native_bridge, rewrite):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    conflict = copy.deepcopy(NATIVE[6])
    conflict["payload"]["item"]["content"][0]["text"] = "conflicting replacement body"
    target = path.with_suffix(".rotated") if rewrite == "replace" else path
    if rewrite == "truncate": path.write_bytes(b"")
    # Force truncation to be detected before appending longer rewritten rows.
    if rewrite == "truncate":
        write_rows(path, [row("session_meta", {"id": TID, "cwd": "/project"})])
        bridge.discover(client.info)
    write_rows(target, ([] if rewrite == "truncate" else [row("session_meta", {"id": TID, "cwd": "/project"})])
               + NATIVE[4:6] + [conflict, NATIVE[7]])
    if rewrite == "replace": target.replace(path)
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert events(bridge) == []
    original, validated, conflicted = bridge.db.execute(
        "SELECT record,validated,conflicted FROM codex_completion_pending").fetchone()
    assert json.loads(original)["text"] == "HERDR_FIFO_RETRY_A_20261013_OK"
    assert (validated, conflicted) == (0, 1)
    # A repeated canonical closure cannot bypass contradictory source content.
    write_rows(path, NATIVE[7:8]); bridge.discover(client.info)
    assert events(bridge) == []


def test_rotation_canonical_closure_without_replayed_candidate_holds_then_revalidates(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    replacement = path.with_suffix(".rotated")
    write_rows(replacement, [row("session_meta", {"id": TID, "cwd": "/project"}), NATIVE[7]])
    replacement.replace(path)
    bridge.discover(client.info)
    assert events(bridge) == []
    assert bridge.db.execute("SELECT validated FROM codex_completion_pending").fetchone()[0] == 0
    write_rows(path, NATIVE[6:8]); bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]


def test_validated_same_inode_truncation_replays_modern_candidate_once(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    inode = path.stat().st_ino
    path.write_bytes(b"")
    write_rows(path, [row("session_meta", {"id": TID, "cwd": "/project"})])
    bridge.discover(client.info)
    assert path.stat().st_ino == inode
    assert events(bridge) == []
    write_rows(path, NATIVE[4:8]); bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"]
    bridge.discover(client.info)
    assert len(events(bridge)) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_certified_turn_dedup_survives_new_item_ID_rotation_and_restart(native_bridge, restart):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:8]); bridge.discover(client.info)
    first_id = events(bridge)[0]["completion"]["event_id"]
    bridge.db.execute("DELETE FROM outbox"); bridge.db.commit()
    if restart:
        dbpath = bridge.db.execute("PRAGMA database_list").fetchone()[2]
        home = bridge.adapters["codex"].source.home
        bridge.close(); bridge.__init__(client, dbpath)
        bridge.adapters["codex"].source.home = home
    final = copy.deepcopy(NATIVE[6]); final["payload"]["item"]["id"] = "new-item-ID-same-closed-turn"
    replacement = path.with_suffix(".new")
    write_rows(replacement, [row("session_meta", {"id": TID, "cwd": "/project"})] + NATIVE[4:6] + [final, NATIVE[7]])
    replacement.replace(path); client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert events(bridge) == []
    assert bridge.db.execute("SELECT event_id FROM codex_completion_turns").fetchall() == [(first_id,)]
    assert bridge.db.execute("SELECT count(*) FROM codex_completions").fetchone()[0] == 1


def test_distinct_native_turns_with_same_final_text_each_complete(native_bridge):
    bridge, client, path, _ = native_bridge
    same_body_B = copy.deepcopy(NATIVE[8:])
    same_body_B[2]["payload"]["item"]["content"][0]["text"] = "HERDR_FIFO_RETRY_A_20261013_OK"
    same_body_B[3]["payload"]["last_agent_message"] = "HERDR_FIFO_RETRY_A_20261013_OK"
    write_rows(path, NATIVE[4:8] + same_body_B); bridge.discover(client.info)
    assert [e["text"] for e in events(bridge)] == ["HERDR_FIFO_RETRY_A_20261013_OK"] * 2
    assert len({e["completion"]["turn_id"] for e in events(bridge)}) == 2
    assert bridge.db.execute("SELECT count(*) FROM codex_completion_turns").fetchone()[0] == 2


def test_turn_certificate_rolls_back_with_failed_ledger_insert(native_bridge, monkeypatch):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters["codex"].source
    write_rows(path, NATIVE[4:7]); bridge.discover(client.info)
    write_rows(path, NATIVE[7:8])
    queue = source.queue_completion
    def fail_after_ledger(record):
        queue(record)
        raise RuntimeError("failure after ledger before transcript commit")
    monkeypatch.setattr(source, "queue_completion", fail_after_ledger)
    bridge.discover(client.info)
    assert events(bridge) == []
    assert not bridge.db.execute("SELECT * FROM codex_completion_turns").fetchall()
    assert not bridge.db.execute("SELECT * FROM codex_completions").fetchall()
    assert bridge.db.execute("SELECT count(*) FROM codex_completion_pending").fetchone()[0] == 1
    monkeypatch.setattr(source, "queue_completion", queue)
    bridge.discover(client.info)
    assert len(events(bridge)) == 1
    assert bridge.db.execute("SELECT count(*) FROM codex_completion_turns").fetchone()[0] == 1


def test_modern_ledger_upgrade_seeds_turn_dedup_without_replaying(native_bridge):
    bridge, client, path, _ = native_bridge
    write_rows(path, NATIVE[4:8]); bridge.discover(client.info)
    event_id = events(bridge)[0]["completion"]["event_id"]
    bridge.db.execute("DELETE FROM outbox")
    bridge.db.execute("DROP TABLE codex_completion_turns"); bridge.db.commit()
    source = CodexRolloutSync(bridge.db, home=bridge.adapters["codex"].source.home)
    bridge.adapters["codex"].source = source
    assert source.db.execute("SELECT event_id FROM codex_completion_turns").fetchone()[0] == event_id
    final = copy.deepcopy(NATIVE[6]); final["payload"]["item"]["id"] = "upgrade-duplicate-item-ID"
    write_rows(path, [final, NATIVE[7]]); bridge.discover(client.info)
    assert events(bridge) == []
