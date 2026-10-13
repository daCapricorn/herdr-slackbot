import copy
import json
from pathlib import Path

import pytest

from herdr_bridge.core import Bridge, BridgeError
from herdr_slackbot.herdr_client import HerdrOutcomeUnknown


class Client:
    def __init__(self):
        self.info = {"pane_id": "w4:p2", "terminal_id": "term-a", "agent": "codex",
                     "agent_status": "blocked", "state_change_seq": 7,
                     "cwd": "/project",
                     "agent_session": {"value": "session-a"}}
        self.screen = (Path(__file__).parent / "fixtures/transcripts/dialog_codex_approval.txt").read_text()
        self.keys = []
        self.prompts = []
        self.error = None

    def list_agents(self):
        return [copy.deepcopy(self.info)]

    def get_agent(self, pane):
        return copy.deepcopy(self.info)

    def process_info(self, pane):
        return {"pane_id": pane, "foreground_processes": [{"pid": 100, "argv": ["codex"]}]}

    def read_agent_once(self, *args, **kwargs):
        return self.screen

    def send_keys(self, pane, keys):
        self.keys.append((pane, keys))
        if self.error:
            raise self.error

    def prompt_agent(self, pane, text):
        self.prompts.append((pane, text))


@pytest.fixture
def setup(tmp_path):
    client = Client()
    bridge = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100, confirmation_timeout=0, notification_delay=0)
    yield client, bridge
    bridge.close()


def request(client, bridge):
    bridge.discover(client.info)
    return bridge.pending()[0]["id"]


def test_discovery_is_durable_and_ack_only_removes_outbox(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    bridge.discover(client.info)
    events = bridge.events()
    assert len(events) == 1 and events[0]["request"]["id"] == token
    assert events[0]["type"] == "approval.requested"
    second = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100)
    assert second.events() == events
    second.ack(events[0]["id"])
    assert second.pending()[0]["id"] == token and not bridge.events()
    second.close()


def test_card_context_survives_delivery_ack_terminal_change_and_restart(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    event = bridge.events()[0]
    bridge.ack(event["id"])
    client.screen = "new terminal contents"
    reopened = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100)
    try:
        context = reopened.result_cards(token)["result"]["card_context"]
        assert context["text"] == event["text"]
        assert context["agent"] == event["agent"]
        assert context["request"]["options"] == event["request"]["options"]
        assert "card_context" not in context["request"]
    finally:
        reopened.close()


def test_answer_one_shot_survives_restart(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    result = bridge.answer(token, 0)
    assert result["outcome"] == "unconfirmed" and client.keys == [("w4:p2", ["1"])]
    again = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100, confirmation_timeout=0)
    assert again.answer(token, 0)["outcome"] == "unconfirmed"
    assert len(client.keys) == 1
    again.discover(client.info)
    assert not again.pending()
    again.close()


@pytest.mark.parametrize("field,value", [
    ("terminal_id", "term-b"), ("agent_session", {"value": "session-b"}),
    ("agent_status", "working"), ("state_change_seq", 9), ("agent", "claude"),
])
def test_old_callback_cannot_target_replacement_agent(setup, field, value):
    client, bridge = setup
    token = request(client, bridge)
    client.info[field] = value
    with pytest.raises(BridgeError):
        bridge.answer(token, 0)
    assert not client.keys


def test_changed_dialog_rejected(setup):
    client, bridge = setup
    token = request(client, bridge)
    client.screen = client.screen.replace("Set-Content", "Remove-Item")
    with pytest.raises(BridgeError, match="Dialog changed"):
        bridge.answer(token, 0)
    assert not client.keys


def test_old_expiry_timestamp_does_not_block_an_exact_live_approval(setup):
    client, bridge = setup
    token = request(client, bridge)
    rec, state = bridge._record(token)
    rec["expires_at"] = 1  # An existing pre-upgrade request already past its old deadline.
    bridge._save(rec,state)
    bridge.clock = lambda: 1001
    assert bridge.answer(token,0)["outcome"] in ("confirmed","unconfirmed")
    assert client.keys == [("w4:p2",["1"])]


def test_lost_reply_does_not_rearm_or_retry(setup):
    client, bridge = setup
    token = request(client, bridge)
    client.error = HerdrOutcomeUnknown("lost reply")
    assert bridge.answer(token, 0)["outcome"] == "unconfirmed"
    bridge.discover(client.info)
    assert bridge.answer(token, 0)["outcome"] == "unconfirmed"
    raw = bridge.db.execute("SELECT record FROM approvals WHERE id=?", (token,)).fetchone()[0]
    assert json.loads(raw)["dispatch"]["error_type"] == "HerdrOutcomeUnknown"
    assert len(client.keys) == 1 and not bridge.pending()


def test_identical_new_dialog_has_new_request_after_transition(setup):
    client, bridge = setup
    token = request(client, bridge)
    bridge.answer(token, 0)
    client.info["state_change_seq"] += 2
    bridge.discover(client.info)
    assert bridge.pending()[0]["id"] != token


def test_unknown_agent_is_not_guessed_as_codex(setup):
    client, bridge = setup
    client.info["agent"] = "pi"
    token = request(client, bridge)
    assert bridge.events()[0]["type"] == "agent.blocked"
    with pytest.raises(BridgeError, match="Unsupported"):
        bridge.answer(token, 0)
    assert not client.keys


def test_sending_cannot_answer_blocked_dialog(setup):
    client, bridge = setup
    with pytest.raises(BridgeError):
        bridge.send("w4:p2", "y")
    client.info["agent_status"] = "idle"
    rejected = bridge.send("w4:p2", "inspect project")
    assert rejected["outcome"] == "rejected" and not client.prompts
    client.screen = """› \x1b[2mAsk Codex to do anything\x1b[0m

  GPT-6.1-Sol medium · /project
  ← for agents · ? for shortcuts
"""
    bridge.send("w4:p2", "inspect project")
    assert client.prompts == [("w4:p2", "inspect project")]
    with pytest.raises(BridgeError):
        bridge.send("project", "hello")


def test_root_manifest_never_starts_legacy_socket_consumer():
    import tomllib
    root = Path(__file__).parent.parent
    manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
    assert manifest["id"] == "herdr-bridge"
    assert not manifest.get("startup")
    assert "herdr_slackbot" not in json.dumps(manifest)


def test_codex_transcript_redraw_does_not_change_approval(setup):
    client, bridge = setup
    token = request(client, bridge)
    client.screen = "• Working (38s)\nNew tool output\n" + client.screen
    bridge.discover(client.info)
    assert len(bridge.events()) == 1
    assert bridge.pending()[0]["id"] == token
    bridge.answer(token, 0)
    assert client.keys == [("w4:p2", ["1"])]


def test_unknown_screen_redraw_notifies_once(setup):
    client, bridge = setup
    client.screen = "Working (5s)"
    token = request(client, bridge)
    client.screen = "Working (10s)"
    bridge.discover(client.info)
    assert len(bridge.events()) == 1 and bridge.pending()[0]["id"] == token
    bridge.clock = lambda: 1001
    client.info["agent_status"] = "working"
    bridge.discover(client.info)
    assert not bridge.events() and not bridge.pending()


def test_replacement_dialog_retires_old_card_and_unsent_event(setup):
    client, bridge = setup
    token = request(client, bridge)
    client.screen = client.screen.replace("Set-Content", "Remove-Item")
    bridge.discover(client.info)
    assert len(bridge.pending()) == 1 and bridge.pending()[0]["id"] != token
    assert len(bridge.events()) == 1
    with pytest.raises(BridgeError):
        bridge.answer(token, 0)
    assert not client.keys


def test_agent_progress_retires_queued_approval(setup):
    client, bridge = setup
    request(client, bridge)
    client.info["agent_status"] = "working"
    assert not bridge.pending() and not bridge.events()


@pytest.mark.parametrize("answering", [False, True])
def test_sequence_change_during_screen_read_fails_closed(setup, answering):
    client, bridge = setup
    token = request(client, bridge) if answering else None

    def advancing_read(*args, **kwargs):
        client.info["state_change_seq"] += 1
        return client.screen

    client.read_agent_once = advancing_read
    if answering:
        with pytest.raises(BridgeError, match="Agent changed before dispatch"):
            bridge.answer(token, 0)
    else:
        bridge.discover(copy.deepcopy(client.info))
    assert not client.keys and not bridge.events()


class VerificationClock:
    def __init__(self):
        self.now = 0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration
        self.on_sleep()


@pytest.fixture
def verifying(tmp_path):
    client = Client()
    timer = VerificationClock()
    bridge = Bridge(client, tmp_path / "verified.sqlite", clock=lambda: 100,
                    monotonic=timer.monotonic, sleep=timer.sleep)
    yield client, bridge, timer
    bridge.close()


def advance(client):
    client.info.update(agent_status="working", state_change_seq=8)
    client.screen = "• Running the approved command"


def test_success_receipt_with_dropped_input_is_not_confirmed_or_retried(verifying, tmp_path):
    client, bridge, timer = verifying
    token = request(client, bridge)
    result = bridge.answer(token, 0)
    assert result["outcome"] == "unconfirmed"
    assert result["verification"]["reason"] == "dialog_still_open"
    assert result["verification"]["elapsed_ms"] == 5000
    assert client.keys == [("w4:p2", ["1"])]
    stored, state = bridge.db.execute("SELECT record,state FROM approvals WHERE id=?", (token,)).fetchone()
    assert state == "unconfirmed"
    assert json.loads(stored)["dispatch"]["option"] == 0
    assert json.loads(stored)["verification"]["observation"]["same_dialog"] is True
    again = Bridge(client, tmp_path / "verified.sqlite", confirmation_timeout=0)
    again.discover(client.info)
    assert not again.pending()
    assert again.answer(token, 0)["outcome"] == "unconfirmed"
    again.close()
    assert len(client.keys) == 1


def test_delayed_acceptance_waits_for_stable_live_evidence(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    timer.on_sleep = lambda: advance(client) if timer.now >= 0.6 else None
    result = bridge.answer(token, 0)
    assert result["outcome"] == "confirmed"
    assert result["verification"]["reason"] == "approval_closed"
    assert result["verification"]["elapsed_ms"] >= 800
    assert len(client.keys) == 1


def test_status_change_alone_cannot_confirm_a_still_visible_approval(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    timer.on_sleep = lambda: client.info.update(agent_status="working", state_change_seq=8)
    result = bridge.answer(token, 0)
    assert result["outcome"] == "unconfirmed"
    assert len(client.keys) == 1


def test_next_approval_is_observed_without_sending_it_any_keys(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    def next_dialog():
        client.screen = client.screen.replace("Set-Content", "Remove-Item")
        client.info["state_change_seq"] = 9
    timer.on_sleep = next_dialog
    result = bridge.answer(token, 0)
    assert result["outcome"] == "confirmed"
    assert result["verification"]["reason"] == "next_dialog"
    bridge.discover(client.info)
    assert bridge.pending()[0]["id"] != token
    assert client.keys == [("w4:p2", ["1"])]


def test_one_transient_frame_is_not_proof_of_acceptance(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    screen = client.screen
    def redraw():
        if timer.now == 0.2:
            advance(client)
        else:
            client.info.update(agent_status="blocked", state_change_seq=7)
            client.screen = screen
    timer.on_sleep = redraw
    assert bridge.answer(token, 0)["outcome"] == "unconfirmed"
    assert len(client.keys) == 1


@pytest.mark.parametrize("failure", ["replace", "read"])
def test_failure_after_write_has_unknown_acceptance_and_never_retries(verifying, failure):
    client, bridge, timer = verifying
    token = request(client, bridge)
    def fail():
        if failure == "replace":
            client.info["terminal_id"] = "term-b"
        else:
            def unreadable(*args, **kwargs):
                raise OSError("read unavailable")
            client.read_agent_once = unreadable
    timer.on_sleep = fail
    result = bridge.answer(token, 0)
    assert result["outcome"] == "uncertain"
    assert result["verification"]["reason"] == ("agent_replaced" if failure == "replace" else "read_failed")
    assert len(client.keys) == 1
    assert bridge.answer(token, 0)["outcome"] == "uncertain"
    assert len(client.keys) == 1


def test_transient_read_error_recovers_without_replaying_write(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    original = client.read_agent_once
    def step():
        if timer.now == 0.2:
            def unreadable(*args, **kwargs):
                raise OSError("temporary read failure")
            client.read_agent_once = unreadable
        else:
            client.read_agent_once = original
            advance(client)
    timer.on_sleep = step
    assert bridge.answer(token, 0)["outcome"] == "confirmed"
    assert len(client.keys) == 1


def test_crash_during_verification_preserves_consumed_authorization(verifying, tmp_path):
    client, bridge, timer = verifying
    token = request(client, bridge)
    def crash(_rec):
        raise SystemExit("worker crashed after writing keys")
    bridge._confirm_answer = crash
    with pytest.raises(SystemExit):
        bridge.answer(token, 0)
    again = Bridge(client, tmp_path / "verified.sqlite", confirmation_timeout=0)
    again.discover(client.info)
    assert again.answer(token, 0)["outcome"] == "unconfirmed"
    assert not again.pending() and len(client.keys) == 1
    again.close()


def test_changed_dialog_without_sequence_progress_is_not_confirmation(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    timer.on_sleep = lambda: setattr(client, "screen", client.screen.replace("Set-Content", "Remove-Item"))
    assert bridge.answer(token, 0)["outcome"] == "uncertain"
    assert len(client.keys) == 1


def test_sequence_regression_cannot_confirm(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    def reset():
        advance(client)
        client.info["state_change_seq"] = 6
    timer.on_sleep = reset
    assert bridge.answer(token, 0)["outcome"] == "uncertain"


def test_confirmed_duplicates_are_cached_even_if_terminal_is_gone(verifying):
    client, bridge, timer = verifying
    token = request(client, bridge)
    timer.on_sleep = lambda: advance(client)
    original = bridge.answer(token, 0)
    client.get_agent = lambda *args: (_ for _ in ()).throw(OSError("gone"))
    assert bridge.answer(token, 0) == original
    with pytest.raises(BridgeError, match="different option"):
        bridge.answer(token, 1)
    assert len(client.keys) == 1


def test_same_click_and_status_return_checking_while_write_is_in_flight(setup):
    import threading
    client, bridge = setup
    token = request(client, bridge)
    entered, release = threading.Event(), threading.Event()
    send = client.send_keys
    def slow_send(*args):
        send(*args)
        entered.set()
        assert release.wait(2)
    client.send_keys = slow_send
    thread = threading.Thread(target=lambda: bridge.answer(token, 0))
    thread.start()
    try:
        assert entered.wait(2)
        assert bridge.answer(token, 0)["outcome"] == "checking"
        assert bridge.check(token)["outcome"] == "checking"
        with pytest.raises(BridgeError, match="different option"):
            bridge.answer(token, 1)
        assert len(client.keys) == 1
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_explicit_retry_has_its_own_attempt_and_duplicate_button_cannot_replay(setup):
    client, bridge = setup
    token = request(client, bridge)
    first = bridge.answer(token, 0)
    second = bridge.retry(token, first["retry_token"])
    assert second["attempt_id"] != first["attempt_id"]
    assert len(client.keys) == 2
    assert bridge.retry(token, first["retry_token"]) == second
    assert bridge.answer(token, 0)["attempt_id"] == second["attempt_id"]
    assert len(client.keys) == 2
    raw = bridge.db.execute("SELECT record FROM approvals WHERE id=?", (token,)).fetchone()[0]
    assert len(json.loads(raw)["attempts"]) == 1


@pytest.mark.parametrize("change", ["dialog", "identity", "sequence"])
def test_retry_rechecks_current_evidence_before_sending(setup, change):
    client, bridge = setup
    token = request(client, bridge)
    result = bridge.answer(token, 0)
    if change == "dialog":
        client.screen = client.screen.replace("Set-Content", "Remove-Item")
    elif change == "identity":
        client.info["terminal_id"] = "replacement"
    elif change == "sequence":
        client.info["state_change_seq"] += 2
    with pytest.raises(BridgeError):
        bridge.retry(token, result["retry_token"])
    assert len(client.keys) == 1


def test_unknown_acceptance_has_no_retry_until_read_only_check_recovers(setup):
    client, bridge = setup
    token = request(client, bridge)
    bridge.answer(token, 0)
    read = client.read_agent_once
    client.read_agent_once = lambda *a, **kw: (_ for _ in ()).throw(OSError("offline"))
    unknown = bridge.check(token)
    assert unknown["outcome"] == "uncertain" and unknown["retry_token"] is None
    with pytest.raises(BridgeError, match="unknown"):
        bridge.retry(token, unknown["attempt_id"])
    client.read_agent_once = read
    assert bridge.check(token)["retry_token"]
    assert len(client.keys) == 1


def test_background_confirmation_and_card_binding_survive_restart(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    binding = {"channel":"slack", "accountId":"default", "conversationId":"D123", "messageTs":"123.456"}
    bridge.bind_card(token, binding)
    bridge.answer(token, 0)
    first_update = next(e for e in bridge.events() if e["type"] == "approval.updated")
    again = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda:100)
    advance(client)
    again.reconcile_answers()
    cards = again.result_cards(token)
    assert cards["result"]["outcome"] == "confirmed" and cards["cards"] == [binding]
    updates = [e for e in again.events() if e["type"] == "approval.updated"]
    assert len(updates) == 1 and updates[0]["id"] != first_update["id"]
    again.ack(first_update["id"])  # A delayed ACK must not delete a newer update.
    assert again.events() == updates
    assert len(client.keys) == 1
    again.close()


def test_crashed_dispatch_resumes_only_observation(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    bridge._confirm_answer = lambda rec: (_ for _ in ()).throw(SystemExit())
    with pytest.raises(SystemExit):
        bridge.answer(token, 0)
    again = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda:100)
    advance(client)
    again.reconcile_answers()
    assert again.result_cards(token)["result"]["outcome"] == "confirmed"
    assert len(client.keys) == 1
    again.close()


def test_background_checks_stop_after_two_minutes_but_live_retry_stays_available(setup):
    client, bridge = setup
    token = request(client, bridge)
    bridge.answer(token, 0)
    bridge.clock = lambda:221
    bridge.reconcile_answers()
    result = bridge.result_cards(token)["result"]
    assert result["watching"] is False
    assert any(e["type"] == "approval.updated" and not e["result"]["watching"] for e in bridge.events())
    assert len(client.keys) == 1
    bridge.clock = lambda:1001
    assert bridge.check(token)["retry_token"] is not None


def test_explicit_retry_gets_its_own_bounded_followup_after_previous_watch_expired(setup):
    client, bridge = setup
    token = request(client, bridge)
    first = bridge.answer(token, 0)
    bridge.clock = lambda:221
    bridge.reconcile_answers()
    second = bridge.retry(token, first["attempt_id"])
    assert second["watching"] is True
    for event in bridge.events():
        bridge.ack(event["id"])
    bridge.clock = lambda:342
    bridge.reconcile_answers()
    updates = [e for e in bridge.events() if e["type"] == "approval.updated"]
    assert len(updates) == 1 and updates[0]["result"]["watching"] is False
    assert updates[0]["result"]["attempt_id"] == second["attempt_id"]
    assert len(client.keys) == 2


def test_same_dialog_at_new_generation_never_offers_retry(setup):
    client, bridge = setup
    token = request(client, bridge)
    bridge.answer(token, 0)
    client.info["state_change_seq"] += 2
    result = bridge.check(token)
    assert result["outcome"] == "unconfirmed" and result["retry_token"] is None
    with pytest.raises(BridgeError):
        bridge.retry(token, result["attempt_id"])
    assert len(client.keys) == 1


def test_reservation_survives_restart_without_sending_keys(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    prepared = bridge.prepare_answer(token, option=0)
    assert prepared["ready_to_dispatch"] and prepared["outcome"] == "checking"
    assert not client.keys
    again = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda:100, confirmation_timeout=0)
    again.reconcile_answers()
    result = again.result_cards(token)["result"]
    assert result["outcome"] == "not_sent" and result["retry_token"] == prepared["attempt_id"]
    assert not client.keys
    again.dispatch_reserved(token, prepared["attempt_id"])
    again.dispatch_reserved(token, prepared["attempt_id"])
    assert len(client.keys) == 1
    again.close()


def test_reservation_revalidates_after_ui_edit(setup):
    client, bridge = setup
    token = request(client, bridge)
    prepared = bridge.prepare_answer(token, option=0)
    client.screen = client.screen.replace("Set-Content", "Remove-Item")
    with pytest.raises(BridgeError, match="Dialog changed"):
        bridge.dispatch_reserved(token, prepared["attempt_id"])
    assert not client.keys
    assert bridge.check(token)["retry_token"] is None


def test_pane_gate_blocks_another_request_while_first_attempt_is_in_flight(setup):
    import threading
    client, bridge = setup
    first = request(client, bridge)
    started, release = threading.Event(), threading.Event()
    send = client.send_keys
    def slow_send(*args):
        send(*args)
        started.set()
        assert release.wait(2)
    client.send_keys = slow_send
    thread = threading.Thread(target=lambda: bridge.answer(first, 0))
    thread.start()
    try:
        assert started.wait(2)
        client.info["state_change_seq"] += 2
        bridge.discover(client.info)
        second = bridge.pending()[0]["id"]
        assert second != first
        with pytest.raises(BridgeError, match="another approval"):
            bridge.prepare_answer(second, option=0)
        assert len(client.keys) == 1
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and not bridge.sending


def test_duplicate_retry_reservation_cannot_make_another_attempt(setup):
    client, bridge = setup
    token = request(client, bridge)
    first = bridge.answer(token, 0)
    prepared = bridge.prepare_answer(token, retry_from=first["retry_token"])
    duplicate = bridge.prepare_answer(token, retry_from=first["retry_token"])
    assert not duplicate["ready_to_dispatch"]
    assert duplicate["attempt_id"] == prepared["attempt_id"]
    bridge.dispatch_reserved(token, prepared["attempt_id"])
    bridge.dispatch_reserved(token, prepared["attempt_id"])
    assert len(client.keys) == 2
