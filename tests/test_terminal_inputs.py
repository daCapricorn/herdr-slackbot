import copy
import json
import threading
from pathlib import Path

import pytest

from herdr_bridge.core import Bridge
from herdr_bridge.codex_session import AUTHORITY_ERROR
from herdr_bridge.terminal_inputs import known_empty_composer


CODEX_EMPTY = """  >_ OpenAI Codex (v0.162.1)

› \x1b[2mAsk Codex to do anything\x1b[0m

  GPT-6.1-Sol medium · /project
  ← for agents · ? for shortcuts ⚠ 1 warning · f2 to view
"""
CLAUDE_EMPTY = """ ▐ Claude Code v2.1.285

────────────────────────
❯
────────────────────────
  Opus 4.1 | Context 2%
"""


class InputClient:
    def __init__(self, *, kind="claude", status="idle"):
        self.info = {"pane_id":"w1:p1", "terminal_id":"term-1", "agent":kind,
                     "agent_status":status, "state_change_seq":1, "cwd":"/project",
                     "agent_session":{"value":"session-1"}}
        self.screen = CLAUDE_EMPTY if kind == "claude" else CODEX_EMPTY
        self.prompts = []
        self.error = None

    def get_agent(self, pane):
        return copy.deepcopy(self.info)

    def list_agents(self):
        return [self.get_agent("w1:p1")]

    def list_panes(self):
        return [copy.deepcopy(self.info)]

    def process_info(self, pane):
        raise OSError("test has no process registry")

    def read_agent_once(self, pane, lines, source="visible", format="text"):
        return self.screen

    def prompt_agent(self, pane, text):
        self.prompts.append((pane,text))
        if self.error:
            raise self.error
        self.info["agent_status"] = "working"
        self.info["state_change_seq"] += 1


def ready_channel(bridge):
    with bridge.lock, bridge.db:
        bridge.channels.save({"id":"binding-1", "account_id":"default", "channel_id":"C1",
            "team_id":"T1", "pane_id":"w1:p1", "terminal_id":"term-1",
            "state":"ready", "closed":False})


def make_bridge(tmp_path, *, kind="claude", status="idle", receipt=True, path="bridge.sqlite"):
    client = InputClient(kind=kind,status=status)
    bridge = Bridge(client,tmp_path/path,confirmation_timeout=0,notification_delay=0)
    ready_channel(bridge)
    source = bridge.adapters[kind].source
    source.receipt_baseline = lambda info: {"source_key":"term-1:session-1", "path":"test",
        "device":1, "inode":1, "offset":0}
    source.receipt_recorded = lambda info,baseline,text: receipt
    return client,bridge


def ingest(bridge, message, text="do the task"):
    return bridge.ingest_input("default","C1",message,"U1",text,thread_id="thread-1",binding_id="binding-1")


def test_known_empty_composer_rejects_drafts_and_stale_empty_prompts():
    assert known_empty_composer("codex",CODEX_EMPTY)
    assert known_empty_composer("codex",CODEX_EMPTY.replace("for shortcuts ⚠ 1 warning · f2 to view","for short  ⚠ 3 · f2"))
    assert known_empty_composer("claude",CLAUDE_EMPTY)
    assert not known_empty_composer("codex",CODEX_EMPTY.replace(
        "› \x1b[2mAsk Codex to do anything\x1b[0m","› drafted text\n  wrapped draft"))
    assert not known_empty_composer("codex",CODEX_EMPTY.replace(
        "› \x1b[2mAsk Codex to do anything\x1b[0m","› \x1b[2mAsk Codex to do anything\x1b[0m\n\n› actual local draft"))
    assert not known_empty_composer("claude",CLAUDE_EMPTY.replace("❯\n","❯ local draft\n"))
    assert known_empty_composer("claude",CLAUDE_EMPTY.replace("Claude Code v2.","Claude Code v3."))
    ghost = '────────────────\n❯\xa0\x1b[0m\x1b[2mTry "fix typecheck errors"\x1b[0m\n────────────────\n  Opus 4.1 | Context 2%\n'
    assert known_empty_composer("claude",ghost)
    assert not known_empty_composer("claude",ghost.replace("\x1b[0m\x1b[2m", ""))


def test_deduplicated_input_is_written_once_and_requires_exact_receipt(tmp_path):
    client,bridge = make_bridge(tmp_path)
    try:
        first = ingest(bridge,"100.1")
        duplicate = ingest(bridge,"100.1","changed redelivery text")
        assert first["outcome"] == duplicate["outcome"] == "confirmed"
        assert client.prompts == [("w1:p1","do the task")]
    finally:
        bridge.close()


def test_busy_fifo_serializes_and_rechecks_empty_composer(tmp_path):
    client,bridge = make_bridge(tmp_path,status="working")
    try:
        assert ingest(bridge,"100.1","first")["outcome"] == "queued"
        assert ingest(bridge,"100.2","second")["outcome"] == "queued"
        client.info.update(agent_status="idle",state_change_seq=2)
        bridge.reconcile_inputs()
        assert client.prompts == [("w1:p1","first")]
        client.info.update(agent_status="idle",state_change_seq=4)
        bridge.reconcile_inputs()
        assert client.prompts == [("w1:p1","first"),("w1:p1","second")]
    finally:
        bridge.close()


def test_blocked_draft_and_changed_session_never_write(tmp_path):
    client,bridge = make_bridge(tmp_path,status="blocked")
    try:
        assert ingest(bridge,"100.1")["reason"] == "agent_blocked_use_approval"
        client.info.update(agent_status="idle",state_change_seq=2)
        client.screen = CLAUDE_EMPTY.replace("❯\n","❯ existing draft\n")
        assert ingest(bridge,"100.2")["reason"] == "composer_not_known_empty"
        client.info.update(agent_status="working",state_change_seq=3)
        assert ingest(bridge,"100.3")["outcome"] == "queued"
        client.info.update(agent_status="idle",state_change_seq=4,
                           agent_session={"value":"replacement"})
        bridge.reconcile_inputs()
        row = bridge.inputs.find("slack","default","C1","100.3")
        assert row["state"] == "rejected" and not client.prompts
    finally:
        bridge.close()


def test_unknown_write_and_restart_are_never_replayed(tmp_path):
    client,bridge = make_bridge(tmp_path,receipt=False)
    client.error = TimeoutError("could contain sensitive transport detail")
    result = ingest(bridge,"100.1")
    assert result["outcome"] == "uncertain" and len(client.prompts) == 1
    bridge.close()
    restarted = Bridge(client,tmp_path/"bridge.sqlite",confirmation_timeout=0)
    try:
        ready_channel(restarted)
        duplicate = ingest(restarted,"100.1")
        assert duplicate["outcome"] == "uncertain" and len(client.prompts) == 1
        record = restarted.inputs.find("slack","default","C1","100.1")
        assert "sensitive" not in str(record)
    finally:
        restarted.close()


def test_approval_arbiter_keeps_prompt_queued(tmp_path):
    client,bridge = make_bridge(tmp_path)
    try:
        bridge.sending["w1:p1"] = "approval-request"
        assert ingest(bridge,"100.1")["outcome"] == "queued"
        assert not client.prompts
    finally:
        bridge.close()


def test_unknown_status_and_binding_terminal_mismatch_fail_before_write(tmp_path):
    client,bridge = make_bridge(tmp_path,status="mystery")
    try:
        result = ingest(bridge,"100.1")
        assert result["outcome"] == "rejected" and result["reason"] == "agent_status_unknown"
        client.info.update(agent_status="idle",terminal_id="replacement")
        with pytest.raises(ValueError,match="terminal identity"):
            ingest(bridge,"100.2")
        assert not client.prompts
    finally:
        bridge.close()


def test_baseline_io_precedes_final_ansi_composer_guard(tmp_path):
    client,bridge = make_bridge(tmp_path)
    try:
        source = bridge.adapters["claude"].source
        def baseline(info):
            client.screen = CLAUDE_EMPTY.replace("❯\n","❯ local draft\n")
            return {"source_key":"term-1:session-1","path":"test","device":1,"inode":1,"offset":0}
        source.receipt_baseline = baseline
        result = ingest(bridge,"100.1")
        assert result["reason"] == "composer_not_known_empty"
        assert not client.prompts
    finally:
        bridge.close()


def test_uncertain_write_blocks_fifo_across_restart(tmp_path):
    client,bridge = make_bridge(tmp_path,receipt=False)
    try:
        client.error = TimeoutError("unknown")
        assert ingest(bridge,"100.1","first")["outcome"] == "uncertain"
        client.error = None
        client.info.update(agent_status="idle",state_change_seq=3)
        assert ingest(bridge,"100.2","second")["outcome"] == "queued"
        bridge.reconcile_inputs()
        assert client.prompts == [("w1:p1","first")]
    finally:
        bridge.close()
    restarted = Bridge(client,tmp_path/"bridge.sqlite",confirmation_timeout=0)
    try:
        ready_channel(restarted)
        restarted.adapters["claude"].source.receipt_recorded = lambda *args: False
        restarted.reconcile_inputs()
        assert client.prompts == [("w1:p1","first")]
        assert restarted.sending["w1:p1"] == "input:1"
    finally:
        restarted.close()


def test_unavailable_baseline_holds_arbiter_until_target_identity_closes(tmp_path):
    client,bridge = make_bridge(tmp_path)
    try:
        source = bridge.adapters["claude"].source
        source.receipt_baseline = lambda info: (_ for _ in ()).throw(OSError("unavailable"))
        assert ingest(bridge,"100.1","first")["outcome"] == "uncertain"
        client.info.update(agent_status="idle",state_change_seq=3)
        assert ingest(bridge,"100.2","second")["outcome"] == "queued"
        bridge.reconcile_inputs()
        assert client.prompts == [("w1:p1","first")]
        assert bridge.sending["w1:p1"] == "input:1"

        client.info.update(terminal_id="replacement",state_change_seq=4)
        bridge.reconcile_inputs()
        first = bridge.inputs.find("slack","default","C1","100.1")
        second = bridge.inputs.find("slack","default","C1","100.2")
        assert first["arbiter_released"] is True
        assert first["reason"] == "target_identity_closed_unconfirmed"
        assert second["state"] == "rejected"
        assert "w1:p1" not in bridge.sending
        assert client.prompts == [("w1:p1","first")]
    finally:
        bridge.close()


def test_queued_terminal_result_is_persisted_for_original_thread(tmp_path):
    client,bridge = make_bridge(tmp_path,status="working")
    try:
        assert ingest(bridge,"100.1","queued prompt")["outcome"] == "queued"
        client.info.update(agent_status="idle",state_change_seq=2)
        bridge.reconcile_inputs()
        event = json.loads(bridge.db.execute(
            "SELECT event FROM outbox WHERE json_extract(event,'$.type')='input.updated'").fetchone()[0])
        assert event["result"]["outcome"] == "confirmed"
        assert event["route"] == {"channel":"slack","accountId":"default",
            "conversationId":"C1","to":"channel:C1","threadId":"thread-1","team_id":"T1"}
    finally:
        bridge.close()


def move_channel(client, bridge, *, terminal="term-1", session="session-1"):
    client.info.update(pane_id="w2:p1", terminal_id=terminal, agent_session={"value":session},
                       agent_status="idle", state_change_seq=3)
    with bridge.lock, bridge.db:
        binding = bridge.channels.from_channel("default", "C1")
        bridge.channels.save({**binding, "pane_id":"w2:p1", "terminal_id":terminal})


def input_updates(bridge):
    return [json.loads(row[0]) for row in bridge.db.execute(
        "SELECT event FROM outbox WHERE json_extract(event,'$.type')='input.updated'")]


def move_with_reused_pane(client):
    moved = {**copy.deepcopy(client.info), "pane_id":"w2:p1",
             "agent_status":"idle", "state_change_seq":3}
    replacement = {**copy.deepcopy(client.info), "terminal_id":"term-2",
                   "agent_session":{"value":"session-2"},
                   "agent_status":"idle", "state_change_seq":3}
    panes = {"w1:p1":replacement, "w2:p1":moved}
    client.info = moved
    client.get_agent = lambda pane: copy.deepcopy(panes[pane])
    client.list_agents = lambda: copy.deepcopy(list(panes.values()))
    client.list_panes = lambda: copy.deepcopy(list(panes.values()))
    return panes


@pytest.mark.parametrize("restart", [False, True])
def test_stale_binding_and_reused_pane_keep_original_unknown_write(tmp_path, restart):
    client, bridge = make_bridge(tmp_path, receipt=False)
    try:
        baseline = bridge.adapters["claude"].source.receipt_baseline
        client.error = TimeoutError("unknown")
        assert ingest(bridge, "100.1", "first")["outcome"] == "uncertain"
        client.error = None
        move_with_reused_pane(client)
        if restart:
            bridge.close()
            bridge = Bridge(client, tmp_path/"bridge.sqlite", confirmation_timeout=0, notification_delay=0)
            bridge.adapters["claude"].source.receipt_baseline = baseline
        seen = []
        def recorded(info, saved, text):
            seen.append((info["pane_id"], text))
            return False
        bridge.adapters["claude"].source.receipt_recorded = recorded
        bridge.reconcile_inputs()
        assert not bridge.inputs.get(1).get("arbiter_released")
        assert seen == [("w2:p1", "first")]
        move_channel(client, bridge)
        assert ingest(bridge, "100.2", "second")["outcome"] == "queued"
        client.info.update(agent_status="blocked", state_change_seq=4)
        client.screen = (Path(__file__).parent/"fixtures/transcripts/dialog_claude_permission.txt").read_text()
        client.keys = []
        client.send_keys = lambda pane, keys: client.keys.append((pane, keys))
        bridge.discover(client.info)
        token = bridge.pending()[0]["id"]
        with pytest.raises(ValueError, match="processing|unresolved"):
            bridge.prepare_answer(token, option=0)
        assert not client.keys
        assert client.prompts == [("w1:p1", "first")]
        client.info.update(agent_status="idle", state_change_seq=5)
        client.screen = CLAUDE_EMPTY
        bridge.adapters["claude"].source.receipt_recorded = lambda info, saved, text: True
        bridge.reconcile_inputs()
        assert bridge.inputs.get(1)["state"] == bridge.inputs.get(2)["state"] == "confirmed"
        assert client.prompts == [("w1:p1", "first"), ("w2:p1", "second")]
        bridge.reconcile_inputs()
        assert len(client.prompts) == 2
    finally:
        bridge.close()


@pytest.mark.parametrize("failure", ["inventory_error", "duplicate_terminal", "duplicate_pane",
    "malformed", "candidate_reused", "agent_unavailable", "authority_unavailable", "identity_missing"])
def test_unknown_input_holds_when_current_terminal_cannot_be_proven(tmp_path, failure):
    client, bridge = make_bridge(tmp_path, receipt=False)
    try:
        client.error = TimeoutError("unknown")
        assert ingest(bridge, "100.1", "first")["outcome"] == "uncertain"
        client.error = None
        panes = move_with_reused_pane(client)
        if failure == "inventory_error":
            client.list_panes = lambda: (_ for _ in ()).throw(OSError("inventory unavailable"))
        elif failure == "duplicate_terminal":
            client.list_panes = lambda: [*copy.deepcopy(list(panes.values())),
                {**copy.deepcopy(client.info), "pane_id":"w3:p1"}]
        elif failure == "duplicate_pane":
            client.list_panes = lambda: [*copy.deepcopy(list(panes.values())),
                {**copy.deepcopy(client.info), "terminal_id":"term-3"}]
        elif failure == "malformed":
            client.list_panes = lambda: [{"pane_id":"w1:p1"}]
        elif failure == "candidate_reused":
            current_inventory = copy.deepcopy(list(panes.values()))
            client.list_panes = lambda: copy.deepcopy(current_inventory)
            panes["w2:p1"] = {**copy.deepcopy(client.info), "terminal_id":"term-3",
                              "agent_session":{"value":"session-3"}}
        elif failure == "agent_unavailable":
            client.list_agents = lambda: [copy.deepcopy(panes["w1:p1"])]
            client.get_agent = lambda pane: (_ for _ in ()).throw(OSError("agent detection unavailable"))
        elif failure == "authority_unavailable":
            client.info.update(agent_session={"value":"replacement"})
            client.info[AUTHORITY_ERROR] = "identity authority unavailable"
        elif failure == "identity_missing":
            client.info["agent_session"] = None
        bridge.reconcile_inputs()
        assert not bridge.inputs.get(1).get("arbiter_released")
        assert bridge.inputs.get(1)["state"] == "uncertain"
        assert client.prompts == [("w1:p1", "first")]
        assert bridge.inputs.blocker({"terminal_id":"term-1"})
    finally:
        bridge.close()


def test_unknown_old_terminal_does_not_block_reused_address_with_unavailable_agent(tmp_path):
    client, bridge = make_bridge(tmp_path, receipt=False)
    try:
        client.error = TimeoutError("unknown")
        assert ingest(bridge, "100.1", "first")["outcome"] == "uncertain"
        client.error = None
        panes = move_with_reused_pane(client)
        current = client.get_agent
        def get_agent(pane):
            if pane == "w2:p1":
                raise OSError("original terminal detection unavailable")
            return current(pane)
        client.get_agent = get_agent
        bridge.reconcile_inputs()
        assert not bridge.inputs.get(1).get("arbiter_released")
        assert "w1:p1" not in bridge.sending
        client.info = panes["w1:p1"]
        with bridge.lock, bridge.db:
            binding = bridge.channels.get("binding-1")
            bridge.channels.save({**binding, "terminal_id":"term-2"})
        assert ingest(bridge, "100.2", "unrelated terminal")["outcome"] == "uncertain"
        assert client.prompts == [("w1:p1", "first"), ("w1:p1", "unrelated terminal")]
        assert not bridge.inputs.get(1).get("arbiter_released")
    finally:
        bridge.close()


@pytest.mark.parametrize("restart", [False, True])
def test_unknown_input_survives_move_and_blocks_new_prompt_and_approval(tmp_path, restart):
    client, bridge = make_bridge(tmp_path, receipt=False)
    try:
        client.error = TimeoutError("unknown")
        assert ingest(bridge, "100.1", "first")["outcome"] == "uncertain"
        client.error = None
        move_channel(client, bridge)
        if restart:
            bridge.close()
            bridge = Bridge(client, tmp_path/"bridge.sqlite", confirmation_timeout=0, notification_delay=0)
            bridge.adapters["claude"].source.receipt_recorded = lambda *args: False
        bridge.reconcile_inputs()
        old = bridge.inputs.get(1)
        assert not old.get("arbiter_released")
        assert ingest(bridge, "100.2", "second")["outcome"] == "queued"
        client.info.update(agent_status="blocked", state_change_seq=4)
        client.screen = (Path(__file__).parent/"fixtures/transcripts/dialog_claude_permission.txt").read_text()
        client.keys = []
        client.send_keys = lambda pane, keys: client.keys.append((pane, keys))
        bridge.discover(client.info)
        token = bridge.pending()[0]["id"]
        with pytest.raises(ValueError, match="processing|unresolved"):
            bridge.prepare_answer(token, option=0)
        assert not client.keys and client.prompts == [("w1:p1", "first")]
        client.info.update(agent_status="idle", state_change_seq=5)
        client.screen = CLAUDE_EMPTY
        seen = []
        def recorded(info, baseline, text):
            seen.append(info["pane_id"])
            return True
        bridge.adapters["claude"].source.receipt_recorded = recorded
        bridge.reconcile_inputs()
        assert bridge.inputs.get(1)["state"] == "confirmed"
        assert client.prompts == [("w1:p1", "first"), ("w2:p1", "second")]
        assert seen and all(pane == "w2:p1" for pane in seen)
        assert "w1:p1" not in bridge.sending
    finally:
        bridge.close()


@pytest.mark.parametrize("replace_terminal", [False, True])
def test_unavailable_baseline_releases_only_definitely_ended_identity(tmp_path, replace_terminal):
    client, bridge = make_bridge(tmp_path, receipt=False)
    try:
        def unavailable(info):
            raise OSError("no receipt baseline")
        bridge.adapters["claude"].source.receipt_baseline = unavailable
        client.error = TimeoutError("unknown")
        assert ingest(bridge, "100.1")["outcome"] == "uncertain"
        original = client.get_agent
        client.get_agent = lambda pane: (_ for _ in ()).throw(OSError("agent read unavailable"))
        bridge.reconcile_inputs()
        assert not bridge.inputs.get(1).get("arbiter_released")
        client.get_agent = original
        client.info[AUTHORITY_ERROR] = "authority temporarily unavailable"
        client.info["agent_session"] = {"value":"replacement"}
        bridge.reconcile_inputs()
        assert not bridge.inputs.get(1).get("arbiter_released")
        client.info.pop(AUTHORITY_ERROR)
        client.error = None
        client.info.update(agent_status="idle", state_change_seq=3)
        if replace_terminal:
            client.info["terminal_id"] = "replacement-terminal"
            with bridge.lock, bridge.db:
                binding = bridge.channels.from_channel("default", "C1")
                bridge.channels.save({**binding, "terminal_id":"replacement-terminal"})
        bridge.reconcile_inputs()
        assert bridge.inputs.get(1)["arbiter_released"]
        assert "w1:p1" not in bridge.sending
        ingest(bridge, "100.2", "new session")
        assert client.prompts == [("w1:p1", "do the task"), ("w1:p1", "new session")]
    finally:
        bridge.close()


@pytest.mark.parametrize("reject", [False, True])
def test_approval_queued_input_emits_one_followup_on_final_disposition(tmp_path, reject):
    client, bridge = make_bridge(tmp_path)
    try:
        bridge.sending["w1:p1"] = "approval-request"
        assert ingest(bridge, "100.1")["outcome"] == "queued"
        assert not client.prompts
        bridge.sending.clear()
        if reject:
            client.screen = CLAUDE_EMPTY.replace("❯\n", "❯ local draft\n")
        bridge.reconcile_inputs()
        bridge.reconcile_inputs()
        updates = input_updates(bridge)
        assert len(updates) == 1
        assert updates[0]["result"]["outcome"] == ("rejected" if reject else "confirmed")
        assert updates[0]["route"]["threadId"] == "thread-1"
        assert updates[0]["route"]["conversationId"] == "C1"
    finally:
        bridge.close()


@pytest.mark.parametrize("write_error", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_receipt_reconcile_during_write_never_regresses_or_duplicates(tmp_path, write_error, closed):
    client, bridge = make_bridge(tmp_path, status="working")
    entered, release = threading.Event(), threading.Event()
    result, failures = [], []
    def write(pane, text):
        client.prompts.append((pane, text))
        entered.set()
        assert release.wait(5)
        if write_error:
            raise TimeoutError("write response lost")
    def dispatch():
        try:
            result.append(bridge._dispatch_input(1))
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=dispatch)
    try:
        assert ingest(bridge, "100.1")["outcome"] == "queued"
        client.info.update(agent_status="idle", state_change_seq=2)
        client.prompt_agent = write
        worker.start()
        assert entered.wait(5)
        if closed:
            client.info["agent_session"] = {"value":"replacement"}
        bridge.reconcile_inputs()
        before = bridge.inputs.get(1)
        expected = "uncertain" if closed else "confirmed"
        assert before["state"] == before["last_notified_state"] == expected
        assert bool(before.get("arbiter_released")) == closed
        assert not bridge.sending
        release.set()
        worker.join(5)
        assert not worker.is_alive() and not failures
        after = bridge.inputs.get(1)
        assert after["state"] == after["last_notified_state"] == expected
        assert bool(after.get("arbiter_released")) == closed
        assert result[0]["outcome"] == expected
        assert len(input_updates(bridge)) == len(client.prompts) == 1
        assert not bridge.sending
    finally:
        release.set()
        if worker.ident:
            worker.join(5)
        bridge.close()
