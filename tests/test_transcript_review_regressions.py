import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from herdr_bridge.core import Bridge
from herdr_bridge.claude_sync import ClaudeTranscriptSync
from herdr_bridge.codex_session import SessionUnavailable
from test_claude_review_regressions import TID, append, answer, user


def test_conversation_event_keeps_transcript_identity_snapshot(tmp_path):
    original = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a",
                "cwd": "/project", "agent_session": {"value": TID}}
    replacement = {**original, "agent_session": {"value": "replacement"}}
    client = SimpleNamespace(list_tabs=lambda *args: [], list_agents=lambda: [replacement])
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    enrich_calls = []

    def changed_enrich(info):
        enrich_calls.append(info)
        return copy.deepcopy(replacement)

    bridge._enrich_agent = changed_enrich
    try:
        bridge._queue_conversation("message-a", original, {"role": "user", "text": "from A"})
        event = json.loads(bridge.db.execute("SELECT event FROM outbox").fetchone()[0])
        assert event["agent"]["session"] == TID
        assert enrich_calls == []
    finally:
        bridge.close()


def test_conversation_display_metadata_uses_snapshot_without_rebinding(tmp_path, monkeypatch):
    original = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a",
                "cwd": "/project", "agent_session": {"value": TID}}
    client = SimpleNamespace(list_tabs=lambda *args: [], list_agents=lambda: [original])
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    monkeypatch.setattr(bridge.adapters["claude"].source, "bound_info",
                        lambda info: pytest.fail("transcript enqueue must not resolve a new session"))
    inspected = []
    monkeypatch.setattr("herdr_bridge.core.session_model",
                        lambda info, **kwargs: inspected.append(copy.deepcopy(info)) or "snapshot-model")
    try:
        bridge._queue_conversation("message-a", original, {"role": "user", "text": "from A"})
        event = json.loads(bridge.db.execute("SELECT event FROM outbox").fetchone()[0])
        assert event["agent"]["session"] == TID
        assert event["agent"]["model"] == "snapshot-model"
        assert inspected == [original]
    finally:
        bridge.close()


def test_unavailable_transcript_defers_completion_until_durable_answer(tmp_path):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    bridge = Bridge(SimpleNamespace(), tmp_path / "bridge.sqlite")
    source = ClaudeTranscriptSync(bridge.db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "idle", "agent_session": {"value": TID}}
    try:
        source.sync(info, bridge._queue_conversation)
        append(path, [user("new question", "u1"), answer("new answer", "a1"),
                      {"type": "system", "subtype": "turn_duration"}])
        saved = path.with_suffix(".saved")
        path.rename(saved)
        source.sync(info, bridge._queue_conversation)
        assert source.diagnostic(info)["state"] == "unavailable"
        assert not source.claim_completion(info)
        saved.rename(path)
        source.sync(info, bridge._queue_conversation)
        assert source.claim_completion(info)
        assert not source.claim_completion(info)
    finally:
        bridge.close()


@pytest.mark.parametrize("claimed", [False, True])
def test_durable_completion_survives_outage_and_restart(tmp_path, claimed):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    state = tmp_path / "state.sqlite"
    db = sqlite3.connect(state)
    source = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "idle", "agent_session": {"value": TID}}
    enqueue = lambda *args: None
    source.sync(info, enqueue)
    append(path, [user("new", "u1"), answer("final", "a1"), {"type": "system", "subtype": "turn_duration"}])
    source.sync(info, enqueue)
    if claimed:
        assert source.claim_completion(info)
    saved = path.with_suffix(".saved")
    path.rename(saved)
    source.sync(info, enqueue)
    assert not source.claim_completion(info)
    db.commit()
    db.close()
    db = sqlite3.connect(state)
    try:
        source = ClaudeTranscriptSync(db, home=tmp_path)
        assert not source.claim_completion(info)
        saved.rename(path)
        source.sync(info, enqueue)
        assert source.claim_completion(info) is (not claimed)
        assert not source.claim_completion(info)
        assert db.execute("SELECT count(*) FROM claude_completion_seen").fetchone()[0] == 1
    finally:
        db.close()


def test_tracked_terminal_defers_new_session_until_its_answer(tmp_path):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    db = sqlite3.connect(":memory:")
    source = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "idle", "agent_session": {"value": TID}}
    enqueue = lambda *args: None
    try:
        source.sync(info, enqueue)
        append(path, [answer("old final", "a1"), {"type": "system", "subtype": "turn_duration"}])
        source.sync(info, enqueue)
        new_tid = "c6565ec4-87cd-4855-b9ee-4a940f248743"
        new_info = {**info, "agent_session": {"value": new_tid}}
        source.sync(new_info, enqueue)
        assert not source.claim_completion(new_info)
        new_path = path.with_name(new_tid + ".jsonl")
        append(new_path, [{**user("old in new session", "new-old"), "sessionId": new_tid}])
        source.sync(new_info, enqueue)
        assert not source.claim_completion(new_info)
        append(new_path, [{**user("new question", "u2"), "sessionId": new_tid},
                          {**answer("new final", "a2"), "sessionId": new_tid},
                          {"type": "system", "subtype": "turn_duration", "sessionId": new_tid}])
        source.sync(new_info, enqueue)
        assert source.claim_completion(new_info)
        assert not source.claim_completion(new_info)
    finally:
        db.close()


@pytest.mark.parametrize("failure", [SessionUnavailable("gone"), OSError("gone")])
def test_resolution_failure_between_scan_and_claim_defers(tmp_path, monkeypatch, failure):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    db = sqlite3.connect(":memory:")
    source = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "idle", "agent_session": {"value": TID}}
    enqueue = lambda *args: None
    try:
        source.sync(info, enqueue)
        append(path, [answer("final", "a1"), {"type": "system", "subtype": "turn_duration"}])
        source.sync(info, enqueue)
        resolve = source._resolve
        with monkeypatch.context() as patch:
            def unavailable(*args):
                raise failure
            patch.setattr(source, "_resolve", unavailable)
            assert not source.claim_completion(info)
        assert source._resolve == resolve
        source.sync(info, enqueue)
        assert source.claim_completion(info)
        assert not source.claim_completion(info)
    finally:
        db.close()


def test_validated_binding_defers_before_first_cursor(tmp_path):
    path = tmp_path / "projects/-project" / f"{TID}.jsonl"
    path.parent.mkdir(parents=True)
    append(path, [user("old", "old")])
    db = sqlite3.connect(":memory:")
    source = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term-a", "cwd": "/project",
            "agent_status": "idle", "agent_session": {"value": TID}}
    try:
        assert source.claim_completion(info)
        source.bind(info, TID)
        assert not db.execute("SELECT * FROM claude_sync_cursors").fetchall()
        assert not source.claim_completion(info)
        assert source.claim_completion({**info, "terminal_id": "never-tracked"})
    finally:
        db.close()


def test_pane_cursor_tracking_matches_terminal_component_exactly(tmp_path):
    db = sqlite3.connect(":memory:")
    source = ClaudeTranscriptSync(db, home=tmp_path)
    info = {"agent": "claude", "pane_id": "w_1:p%1"}
    try:
        db.execute("INSERT INTO claude_sync_cursors VALUES (?, ?, ?, ?, ?)",
                   (info["pane_id"] + ":" + TID, "missing", 0, 0, 0))
        assert not source.claim_completion(info)
        assert source.claim_completion({**info, "pane_id": "w_1"})
        assert source.claim_completion({**info, "pane_id": "wa1:pz1"})
    finally:
        db.close()
