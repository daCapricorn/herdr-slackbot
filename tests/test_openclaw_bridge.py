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
                     "agent_session": {"value": "session-a"}}
        self.screen = (Path(__file__).parent / "fixtures/transcripts/dialog_codex_approval.txt").read_text()
        self.keys = []
        self.prompts = []
        self.error = None

    def list_agents(self):
        return [copy.deepcopy(self.info)]

    def get_agent(self, pane):
        return copy.deepcopy(self.info)

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
    bridge = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100)
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


def test_answer_one_shot_survives_restart(setup, tmp_path):
    client, bridge = setup
    token = request(client, bridge)
    result = bridge.answer(token, 0)
    assert result["outcome"] == "dispatched" and client.keys == [("w4:p2", ["1"])]
    again = Bridge(client, tmp_path / "bridge.sqlite", clock=lambda: 100)
    with pytest.raises(BridgeError, match="already answered"):
        again.answer(token, 0)
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


def test_expired_and_invalid_options_are_rejected(setup):
    client, bridge = setup
    token = request(client, bridge)
    for option in (-1, 100, True, "0"):
        with pytest.raises(BridgeError):
            bridge.answer(token, option)
    bridge.clock = lambda: 1001
    with pytest.raises(BridgeError, match="expired"):
        bridge.answer(token, 0)
    assert not client.keys


def test_lost_reply_does_not_rearm_or_retry(setup):
    client, bridge = setup
    token = request(client, bridge)
    client.error = HerdrOutcomeUnknown("lost reply")
    with pytest.raises(HerdrOutcomeUnknown):
        bridge.answer(token, 0)
    bridge.discover(client.info)
    with pytest.raises(BridgeError):
        bridge.answer(token, 0)
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
