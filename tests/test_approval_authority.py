import copy

import pytest

from herdr_bridge.codex_session import AUTHORITY_ERROR
from herdr_bridge.core import Bridge, BridgeError, identity
from test_codex_questions import COLLAPSED, QuestionSource
from test_openclaw_bridge import Client, VerificationClock


def unavailable(client):
    client.info["agent_session"] = None
    client.info[AUTHORITY_ERROR] = "Foreground session authority unavailable"


@pytest.fixture(params=["approval", "question"])
def target(request, tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('[tui]\nterminal_title = ["thread"]\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    client = Client()
    client.info["agent_session"] = None
    source = QuestionSource() if request.param == "question" else None
    if source:
        client.info["agent_status"] = "working"
        client.screen = COLLAPSED
    core = Bridge(client, tmp_path / "bridge.sqlite", question_source=source,
                  confirmation_timeout=0, notification_delay=0)
    core.discover(client.info)
    token = core.pending()[0]["id"]
    event = core.events()[0]
    yield client, core, token, event, source
    core.close()


@pytest.mark.parametrize("stage", ["reserve", "dispatch"])
@pytest.mark.parametrize("snapshot", ["initial", "fresh"])
def test_unknown_authority_blocks_reservation_and_final_dispatch(target, stage, snapshot):
    client, core, token, _, _ = target
    prepared = core.prepare_answer(token, option=0) if stage == "dispatch" else None
    original_identity = identity(client.info)
    if snapshot == "initial":
        unavailable(client)
    else:
        def read(*args, **kwargs):
            unavailable(client)
            return client.screen
        client.read_agent_once = read
    with pytest.raises(BridgeError, match="session identity unavailable"):
        if prepared:
            core.dispatch_reserved(token, prepared["attempt_id"])
        else:
            core.prepare_answer(token, option=0)
    assert identity(client.info) == original_identity
    assert not client.keys and not client.prompts
    _, state = core._record(token)
    assert state == ("reserved" if prepared else "pending")


@pytest.mark.parametrize("snapshot", ["initial", "fresh"])
def test_unknown_authority_defers_attention_handoff_and_retires_pending_card(target, snapshot):
    client, core, token, event, source = target
    if snapshot == "initial":
        unavailable(client)
    elif source:
        original = source.snapshot
        def question_snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            unavailable(client)
            return result
        source.snapshot = question_snapshot
    else:
        def read(*args, **kwargs):
            unavailable(client)
            return client.screen
        client.read_agent_once = read
    assert core.event_for_delivery(event["id"]) is None
    assert core._record(token)[1] == "pending"
    assert core.pending() == []
    assert core.events() == []
    assert core._record(token)[1] == "superseded"
    assert not client.keys and not client.prompts


@pytest.mark.parametrize("snapshot", ["initial", "fresh"])
def test_unknown_authority_blocks_prompt_send(target, snapshot):
    client, core, _, _, _ = target
    client.info["agent_status"] = "idle"
    if snapshot == "initial":
        unavailable(client)
    else:
        original = core._agent
        def agent(pane):
            info = original(pane)
            unavailable(client)
            return info
        core._agent = agent
    with pytest.raises(BridgeError, match="session identity unavailable"):
        core.send(client.info["pane_id"], "inspect project")
    assert not client.keys and not client.prompts


def test_unknown_authority_keeps_passive_agent_reads_available(target):
    client, core, _, _, _ = target
    unavailable(client)
    assert core._agent(client.info["pane_id"])[AUTHORITY_ERROR]
    assert core.read(client.info["pane_id"])["text"] == client.screen
    assert isinstance(core.sync_status(client.info["pane_id"]), dict)


def test_question_discovery_rejects_unknown_authority(target):
    client, core, _, _, _ = target
    unavailable(client)
    assert core.discover_questions(copy.deepcopy(client.info)) == {}


@pytest.mark.parametrize("snapshot", ["initial", "fresh"])
def test_authority_outage_after_dispatch_cannot_confirm_or_rearm(target, snapshot):
    client, core, token, _, source = target
    timer = VerificationClock()
    core.monotonic, core.sleep = timer.monotonic, timer.sleep
    core.confirmation_timeout = 0.4
    process_info = client.process_info
    read = client.read_agent_once
    dispatched, outage = False, False

    def process(pane):
        if outage:
            raise OSError("Foreground process observation failed")
        return process_info(pane)

    def sent(pane, value):
        nonlocal dispatched, outage
        (client.prompts if source else client.keys).append((pane, value))
        dispatched = True
        outage = snapshot == "initial"
        client.info.update(agent_status="working", state_change_seq=8)
        if source:
            source.answers = {core._record(token)[0]["question"]["id"]: "Purple"}
        else:
            client.screen = "Running the approved command"

    def reading(*args, **kwargs):
        nonlocal outage
        result = read(*args, **kwargs)
        if dispatched and snapshot == "fresh":
            outage = True
        return result

    client.process_info = process
    client.read_agent_once = reading
    if source:
        original = source.snapshot
        def question_snapshot(*args, **kwargs):
            nonlocal outage
            result = original(*args, **kwargs)
            if dispatched and snapshot == "fresh":
                outage = True
            return result
        source.snapshot = question_snapshot
        client.prompt_agent = sent
    else:
        client.send_keys = sent
    prepared = core.prepare_answer(token, option=0)
    result = core.dispatch_reserved(token, prepared["attempt_id"])
    assert result["outcome"] == "uncertain"
    assert result["verification"]["reason"] == "session_identity_unavailable"
    assert result["verification"]["retryable"] is False
    assert result["retry_token"] is None
    assert core._agent(client.info["pane_id"])[AUTHORITY_ERROR]
    stored, state = core._record(token)
    assert state == "uncertain" and stored["verification"] == result["verification"]
    assert core.answer(token, 0)["outcome"] == "uncertain"
    assert len(client.keys) + len(client.prompts) == 1
