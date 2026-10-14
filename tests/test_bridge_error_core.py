"""Integration checks for the separately delivered core error-callsite patch."""
import json
from types import SimpleNamespace

import pytest

from herdr_bridge import core
from herdr_bridge.__main__ import response_for_request
from herdr_bridge.codex_session import AUTHORITY_ERROR
from herdr_bridge.errors import BridgeError


@pytest.fixture
def bridge(tmp_path):
    client = SimpleNamespace(list_panes=lambda: [{"pane_id": "invalid"}])
    instance = core.Bridge(client, tmp_path / "bridge.sqlite")
    yield instance
    instance.close()


def test_core_uses_shared_value_error_class():
    assert core.BridgeError is BridgeError


def test_stale_submission_keeps_attempt_identity_and_proves_no_dispatch(bridge):
    record = {"id": "request", "dispatch": {"attempt_id": "current"}}
    bridge.db.execute("INSERT INTO approvals VALUES (?,?,?)", ("request", json.dumps(record), "reserved"))
    bridge.db.commit()
    response = response_for_request(json.dumps({"id": "rpc", "method": "dispatch_reserved",
        "params": {"request_id": "request", "attempt_id": "stale"}}), {"dispatch_reserved": bridge.dispatch_reserved})
    assert response["error"] == {
        "code": "stale_submission", "message": "Submission is stale; nothing was sent",
        "details": {"request_id": "request", "attempt_id": "stale"}, "outcome": "not_sent",
    }
    assert bridge._record("request") == (record, "reserved")


def test_invalid_prompt_is_pre_write_guard_without_changing_human_message(bridge):
    response = response_for_request('{"id":"rpc","method":"send","params":{"pane":"w4:p2","text":""}}',
                                    {"send": bridge.send})
    assert response["error"] == {"code": "invalid_prompt", "details": {},
        "message": "Prompt must contain 1–16000 characters", "outcome": "not_sent"}


def test_post_attempt_identity_and_inventory_helpers_remain_unknown(bridge, monkeypatch):
    with pytest.raises(BridgeError) as caught:
        core.require_session_authority({AUTHORITY_ERROR: "unavailable"})
    assert caught.value.code == "session_identity_unavailable"
    assert caught.value.outcome == "unknown"
    with pytest.raises(BridgeError) as caught:
        bridge._input_target_info({"terminal_id": "term", "pane_id": "w4:p2"})
    assert caught.value.code == "terminal_inventory_unavailable"
    assert caught.value.outcome == "unknown"
    monkeypatch.setattr(bridge, "_input_target_info", lambda rec: None)
    with pytest.raises(BridgeError) as caught:
        bridge._revalidate_started_input({})
    assert caught.value.code == "agent_identity_changed"
    assert caught.value.outcome == "unknown"


def test_request_absence_never_claims_an_earlier_attempt_was_not_sent(bridge):
    response = response_for_request('{"id":"rpc","method":"check","params":{"request_id":"gone"}}',
                                    {"check": bridge.check})
    assert response["error"]["code"] == "request_absent"
    assert response["error"]["details"] == {"request_id": "gone"}
    assert response["error"]["outcome"] == "unknown"
