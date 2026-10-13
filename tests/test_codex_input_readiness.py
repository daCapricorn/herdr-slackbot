"""FIFO readiness differs from Codex's capacity to steer an active turn."""
import json
from pathlib import Path

import pytest

from test_codex_fresh_input import fresh, native_path
from test_codex_title_identity import A, meta, rows, turn
from test_terminal_inputs import ingest
from herdr_bridge.core import Bridge


def persist(home, body=()):
    path = native_path(home.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rows(meta(A, "/project"), *body))
    home.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", (A, str(path), "/project", 0, None, "gpt-db"))
    home.db.commit()
    return path


def close_turn(tid):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": tid}}


def append(path, *values):
    with path.open("a") as stream:
        stream.write(rows(*values))


def receipt(tid, text):
    return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": A,
        "turn_id": tid, "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}


def start(tid):
    return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": tid}}


def answer(tid, text):
    return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": tid,
        "item": {"type": "AgentMessage", "id": f"answer-{tid}", "phase": "final_answer", "text": text}}}


@pytest.mark.parametrize("herdr_status", ["idle", "done"])
@pytest.mark.parametrize("persisted", [False, True])
def test_native_active_startup_queues_even_with_empty_composer(fresh, herdr_status, persisted):
    home, native, client, bridge = fresh
    if persisted:
        persist(home, turn(1, "owner HOLD"))
    client.agents["w1:p1"]["agent_status"] = herdr_status
    native.thread["status"] = {"type": "active", "activeFlags": []}
    assert native.thread["canAcceptDirectInput"] is True
    assert ingest(bridge, "100.1", "A")["outcome"] == "queued"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    bridge.reconcile_inputs()
    assert client.prompts == []


def test_native_active_during_baseline_cannot_reserve(fresh, monkeypatch):
    _, native, client, bridge = fresh
    source = bridge.adapters["codex"].source
    baseline = source.receipt_baseline
    def race(info):
        result = baseline(info)
        native.thread["status"] = {"type": "active"}
        return result
    monkeypatch.setattr(source, "receipt_baseline", race)
    assert ingest(bridge, "100.1")["outcome"] == "queued"
    assert client.prompts == []


def test_native_idle_without_durable_owner_closure_stays_queued(fresh):
    home, _, client, bridge = fresh
    persist(home, turn(1, "owner HOLD"))
    assert ingest(bridge, "100.1", "A")["outcome"] == "queued"
    bridge.reconcile_inputs()
    assert client.prompts == []


@pytest.mark.parametrize("restart", [False, True])
def test_startup_fifo_releases_only_after_each_exact_native_turn_closes(fresh, restart):
    home, native, client, bridge = fresh
    path = persist(home, [start("HOLD"), receipt("HOLD", "owner HOLD")])
    native.thread["status"] = {"type": "active"}
    assert ingest(bridge, "100.1", "A")["outcome"] == "queued"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    assert not client.prompts
    # The native task can become idle before its closure flushes to disk.
    native.thread["status"] = {"type": "idle"}
    bridge.reconcile_inputs()
    assert not client.prompts
    append(path, answer("HOLD", "HOLD final"), close_turn("HOLD"))
    def prompt(pane, text):
        client.prompts.append((pane, text))
        append(path, start(text), receipt(text, text))
        native.thread["status"] = {"type": "active"}
        # Herdr stays idle throughout model startup, reproducing the live race.
    client.prompt_agent = prompt
    reader = bridge
    if restart:
        reader = Bridge(client, home.path.parent / "bridge.sqlite", confirmation_timeout=0, notification_delay=0)
    try:
        reader.sync_conversation(client.get_agent("w1:p1"))
        reader.reconcile_inputs()
        assert client.prompts == [("w1:p1", "A")]
        first = reader.inputs.find("slack", "default", "C1", "100.1")
        assert first["state"] == "confirmed"  # Receipt, independently of turn closure.
        assert reader.inputs.find("slack", "default", "C1", "100.2")["state"] == "queued"
        reader.reconcile_inputs()
        assert len(client.prompts) == 1
        native.thread["status"] = {"type": "idle"}
        append(path, close_turn("HOLD"))  # A late old closure cannot release B.
        reader.reconcile_inputs()
        assert len(client.prompts) == 1
        append(path, answer("A", "A final"), close_turn("A"))
        reader.sync_conversation(client.get_agent("w1:p1"))
        reader.reconcile_inputs()
        assert client.prompts == [("w1:p1", "A"), ("w1:p1", "B")]
        native.thread["status"] = {"type": "idle"}
        append(path, answer("B", "B final"), close_turn("B"))
        reader.sync_conversation(client.get_agent("w1:p1"))
        reader.sync_conversation(client.get_agent("w1:p1"))
        events = [json.loads(row[0]) for row in reader.db.execute("SELECT event FROM outbox")]
        for kind in ("conversation.message", "agent.done"):
            final_texts = [event.get("text") for event in events if event["type"] == kind]
            assert final_texts.count("A final") == 1
            assert final_texts.count("B final") == 1
        assert [event.get("text") for event in events].count("A") == 1
        assert [event.get("text") for event in events].count("B") == 1
        assert reader.inputs.find("slack", "default", "C1", "100.2")["state"] == "confirmed"
    finally:
        if reader is not bridge:
            reader.close()


@pytest.mark.parametrize("restart", [False, True])
def test_legacy_receipt_before_lifecycle_start_does_not_release_next_input(fresh, restart):
    home, native, client, bridge = fresh
    path = persist(home)
    def prompt(pane, text):
        client.prompts.append((pane, text))
        append(path, {"type": "event_msg", "payload": {"type": "user_message", "message": text}})
    client.prompt_agent = prompt
    assert ingest(bridge, "100.1", "A")["outcome"] == "confirmed"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    reader = Bridge(client, home.path.parent / "bridge.sqlite", confirmation_timeout=0) if restart else bridge
    try:
        reader.reconcile_inputs()
        assert client.prompts == [("w1:p1", "A")]
        append(path, start("A"), receipt("wrong", "A"), close_turn("A"))
        reader.reconcile_inputs()
        assert len(client.prompts) == 1
        append(path, start("A2"), receipt("A2", "A"), close_turn("A2"))
        reader.reconcile_inputs()
        assert client.prompts == [("w1:p1", "A"), ("w1:p1", "B")]
    finally:
        if reader is not bridge:
            reader.close()


@pytest.mark.parametrize("native_status", ["active", "systemError"])
def test_command_send_obeys_native_readiness(fresh, native_status):
    _, native, client, bridge = fresh
    native.thread["status"] = {"type": native_status}
    assert bridge.send("w1:p1", "command task")["outcome"] == "queued"
    assert not client.prompts


def test_native_active_during_final_screen_read_cannot_reserve(fresh):
    _, native, client, bridge = fresh
    def screen(*args, **kwargs):
        native.thread["status"] = {"type": "active"}
        return client.screen
    client.read_agent_once = screen
    assert ingest(bridge, "100.1")["outcome"] == "queued"
    assert not client.prompts


@pytest.mark.parametrize("closure", [True, False])
def test_large_initial_tail_requires_native_idle_and_exact_closure(fresh, closure):
    home, native, _, bridge = fresh
    filler = [{"type": "response_item", "payload": {"text": "x" * (1024 * 1024 - 100)}}] * 18
    path = persist(home, [start("long"), *filler, *([close_turn("long")] if closure else [])])
    source = bridge.adapters["codex"].source
    info = bridge._agent("w1:p1")
    assert source.input_blocker(info) == (None if closure else "codex_native_readiness_unavailable")
    state = json.loads(bridge.db.execute("SELECT record FROM codex_input_readiness").fetchone()[0])
    assert state["offset"] == path.stat().st_size
    assert source.input_blocker(info) == (None if closure else "codex_native_readiness_unavailable")
    native.thread["status"] = {"type": "active"}
    assert source.input_blocker(bridge._agent("w1:p1")) == "codex_native_busy"


@pytest.mark.parametrize("change", ["partial", "corrupt", "replace", "rewrite", "truncate"])
def test_unavailable_or_compacted_prior_turn_never_releases_fifo(fresh, change):
    home, _, client, bridge = fresh
    path = persist(home)
    def prompt(pane, text):
        client.prompts.append((pane, text))
        append(path, start("A"), receipt("A", text))
    client.prompt_agent = prompt
    assert ingest(bridge, "100.1", "A")["outcome"] == "confirmed"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    if change == "partial":
        with path.open("a") as stream:
            stream.write(json.dumps(close_turn("A")))
    elif change == "corrupt":
        with path.open("a") as stream:
            stream.write("invalid-json\n")
    else:
        # Identity-valid compaction still cannot prove the old request closed.
        body = rows(meta(A, "/project"), start("other"), close_turn("other"))
        if change == "replace":
            replacement = path.with_suffix(".tmp")
            replacement.write_text(body)
            replacement.replace(path)
        elif change == "rewrite":
            path.write_text(body + rows({"type": "response_item", "payload": {"text": "padding" * 300}}))
        else:
            path.write_text(body)
    bridge.reconcile_inputs()
    assert client.prompts == [("w1:p1", "A")]
    assert bridge.inputs.find("slack", "default", "C1", "100.2")["state"] == "queued"


def test_complete_large_tool_output_does_not_stall_lifecycle_cursor(fresh):
    home, _, _, bridge = fresh
    path = persist(home, [start("owner"), {"type": "response_item", "payload": {
        "type": "function_call_output", "output": "x" * (2 * 1024 * 1024)}}, close_turn("owner")])
    source = bridge.adapters["codex"].source
    assert source.input_blocker(bridge._agent("w1:p1")) is None
    state = json.loads(bridge.db.execute("SELECT record FROM codex_input_readiness").fetchone()[0])
    assert state["offset"] == path.stat().st_size
    assert source.input_blocker(bridge._agent("w1:p1")) is None


def test_record_exceeding_scan_budget_is_not_skipped_as_turn_evidence(fresh):
    home, _, client, bridge = fresh
    path = persist(home)
    def prompt(pane, text):
        client.prompts.append((pane, text))
        append(path, start("A"), receipt("A", text))
    client.prompt_agent = prompt
    assert ingest(bridge, "100.1", "A")["outcome"] == "confirmed"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    append(path, {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "A",
        "last_agent_message": "x" * (17 * 1024 * 1024)}})
    bridge.reconcile_inputs()
    assert client.prompts == [("w1:p1", "A")]
    assert bridge.inputs.find("slack", "default", "C1", "100.2")["state"] == "queued"


def test_wrong_thread_closure_cannot_release_owner_or_prior_receipt(fresh):
    home, _, client, bridge = fresh
    path = persist(home)
    def prompt(pane, text):
        client.prompts.append((pane, text))
        append(path, start("A"), receipt("A", text))
    client.prompt_agent = prompt
    assert ingest(bridge, "100.1", "A")["outcome"] == "confirmed"
    assert ingest(bridge, "100.2", "B")["outcome"] == "queued"
    append(path, {"type": "event_msg", "payload": {"type": "task_complete",
        "turn_id": "A", "thread_id": "other-thread"}})
    bridge.reconcile_inputs()
    assert client.prompts == [("w1:p1", "A")]
    append(path, close_turn("A"))
    bridge.reconcile_inputs()
    assert client.prompts == [("w1:p1", "A"), ("w1:p1", "B")]


def test_draft_created_during_final_native_read_is_rejected(fresh):
    _, native, client, bridge = fresh
    captured = []
    def screen(*args, **kwargs):
        captured.append(True)
        return client.screen
    def race(observer):
        if captured:
            client.screen = client.screen.replace("\x1b[2mAsk Codex to do anything\x1b[0m", "local draft")
    client.read_agent_once = screen
    native.race = race
    assert ingest(bridge, "100.1", "remote A")["reason"] == "composer_not_known_empty"
    assert not client.prompts


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("identity", ["modern", "legacy", "wrong_thread", "missing_turn"])
def test_upgrade_midturn_receipt_baseline_requires_exact_modern_identity(fresh, restart, identity):
    home, native, client, bridge = fresh
    # Captured native HOLD/A/B startup failure: the B reservation's cutoff
    # follows HOLD's task_started and A's receipt, so no later start exists.
    fixture = Path(__file__).parent / "fixtures/transcripts/codex_fifo_native_startup.jsonl"
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    for row in records:
        if "thread_id" in row["payload"]:
            row["payload"]["thread_id"] = A
    path = persist(home, records[:3])
    source = bridge.adapters["codex"].source
    info = bridge._agent("w1:p1")
    baseline = source.receipt_baseline(info)
    prompt = records[3]["payload"]["item"]["content"][0]["text"]
    with bridge.lock, bridge.db:
        prior = bridge.inputs.insert({"source": "command", "account_id": "local", "channel_id": "w1:p1",
            "message_ts": "old-B", "sender_id": "authorized-command", "thread_id": None,
            "binding": None, "prompt": prompt, "receipt_baseline": baseline, "attempt_id": "old-attempt",
            **bridge._input_snapshot(info)}, state="confirmed")
    if identity == "legacy":
        records[3]["payload"] = {"type": "user_message", "message": prompt}
    elif identity == "wrong_thread":
        records[3]["payload"]["thread_id"] = "other-thread"
    elif identity == "missing_turn":
        records[3]["payload"].pop("turn_id")
    append(path, records[3])
    reader = Bridge(client, home.path.parent / "bridge.sqlite", confirmation_timeout=0) if restart else bridge
    try:
        native.thread["status"] = {"type": "active"}
        current = reader._agent("w1:p1")
        assert reader.adapters["codex"].source.input_blocker(current, prior) == "codex_native_busy"
        native.thread["status"] = {"type": "idle"}
        # Runtime idle alone cannot erase the durable unmatched HOLD start.
        assert reader.adapters["codex"].source.input_blocker(reader._agent("w1:p1"), prior) == "codex_native_busy"
        append(path, *records[4:])
        blocker = reader.adapters["codex"].source.input_blocker(reader._agent("w1:p1"), prior)
        assert blocker == (None if identity == "modern" else "codex_native_turn_unsettled")
        if identity == "modern":
            # The exact closure must not hide a subsequent unmatched start.
            append(path, start("next-owner"))
            assert reader.adapters["codex"].source.input_blocker(reader._agent("w1:p1"), prior) == "codex_native_busy"
    finally:
        if reader is not bridge:
            reader.close()
