import json

import pytest

from herdr_bridge.__main__ import response_for_request
from herdr_bridge.errors import BridgeError, error_payload
from herdr_bridge.herdr_client import HerdrError, HerdrUnavailable


def test_bridge_error_retains_value_error_compatibility_and_safe_default():
    error = BridgeError("Nothing was sent")
    assert isinstance(error, ValueError)
    assert str(error) == "Nothing was sent"
    assert error_payload(error) == {
        "code": "bridge_error", "message": "Nothing was sent", "details": {}, "outcome": "unknown",
    }
    with pytest.raises(ValueError, match="Unsupported"):
        BridgeError("bad", outcome="confirmed")


def test_guard_error_round_trips_code_details_outcome_and_message():
    details = {"request_id": "request", "attempt_id": "attempt"}

    def guard(**params):
        raise BridgeError("Submission is stale; nothing was sent", code="stale_submission",
                          details=details, outcome="not_sent")

    response = response_for_request(json.dumps({"id": "rpc", "method": "dispatch_reserved", "params": details}),
                                    {"dispatch_reserved": guard})
    assert json.loads(json.dumps(response)) == {"id": "rpc", "error": {
        "code": "stale_submission", "message": "Submission is stale; nothing was sent",
        "details": details, "outcome": "not_sent",
    }}


@pytest.mark.parametrize("error", [RuntimeError("disk full"), HerdrError("agent_not_found", "gone"),
                                      HerdrUnavailable("pipe gone"), BridgeError("nothing was sent")])
def test_exceptions_after_attempt_do_not_infer_no_write_from_code_or_message(error):
    attempts = []

    def write(**params):
        attempts.append(params)
        raise error

    response = response_for_request('{"id":"rpc","method":"send","params":{"text":"prompt"}}', {"send": write})
    assert response["error"]["code"] == getattr(error, "code", "bridge_error")
    assert response["error"]["message"] == str(error)
    assert response["error"]["outcome"] == "unknown"
    assert attempts == [{"text": "prompt"}]


@pytest.mark.parametrize("line, code", [
    ('{"id":"rpc","method":"absent"}', "unknown_method"),
    ('{"id":"rpc","method":"send","params":[]}', "invalid_request"),
    ('{"id":"rpc"}', "invalid_request"),
    ('[]', "invalid_request"),
    ('invalid-json', "invalid_request"),
])
def test_invalid_requests_are_proven_not_invoked(line, code):
    def write(**params):
        pytest.fail("Invalid request reached terminal method")

    response = response_for_request(line, {"send": write})
    assert response["error"]["code"] == code
    assert response["error"]["outcome"] == "not_sent"


def test_provider_acknowledgement_does_not_become_terminal_confirmation():
    response = response_for_request('{"id":"rpc","method":"send"}',
                                    {"send": lambda: {"acknowledged": True, "outcome": "uncertain"}})
    assert response == {"id": "rpc", "result": {"acknowledged": True, "outcome": "uncertain"}}
