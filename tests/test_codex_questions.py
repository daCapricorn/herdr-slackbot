import copy
import json
import sqlite3

import pytest

from herdr_bridge.core import Bridge, BridgeError
from herdr_bridge.questions import CodexQuestionSource, QuestionUnavailable, input_plan, question_id, reply_text
from test_openclaw_bridge import Client


COLLAPSED = "• Working (10s)\n\n• Queued follow-up inputs\n  ? 1 question · 15s\n    ⇧← to answer\n\n› Ask Codex to do anything\n\n GPT-6.1-Sol · /project"
QUESTION = {"id": question_id("call-1", 0), "title": "Choose a harmless test label", "options": ["Purple", "Green"],
            "thread_id": "session-a", "turn_id": "turn-a"}
EXPANDED = "• Working\n\n1 of 1\nChoose a harmless test label\n\n› 1. Purple\n  2. Green\n  3. Other\n\nenter submit   esc skip   ⇧→ main prompt"


def append(path, rows):
    with path.open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def event(kind, **data):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": "turn-a", **data}}


def question_event(questions=None):
    return event("item_completed", item={"type": "AgentMessage", "id": "call-1", "delivery": "async",
                "questions": questions or [{"title": QUESTION["title"], "options": QUESTION["options"]}]})


def user_reply(question=QUESTION, answer="Purple"):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": reply_text(question, answer)}]}}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "rollout.jsonl"
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER,agent_path TEXT)")
    db.execute("INSERT INTO threads VALUES ('session-a',?,'/project',0,NULL)", (str(path),))
    db.commit()
    append(path, [{"type": "session_meta", "payload": {"id": "session-a", "cwd": "/project"}},
                  event("task_started"), question_event()])
    yield CodexQuestionSource(tmp_path), path, db
    db.close()


def info(session=True):
    data = {"agent": "codex", "cwd": "/project"}
    if session:
        data["agent_session"] = {"value": "session-a"}
    return data


def test_structured_question_identity_and_multiple_questions(source):
    reader, path, _ = source
    append(path, [event("item_completed", item={"type": "AgentMessage", "id": "call-2", "delivery": "async",
                  "questions": [{"title": "First", "options": None}, {"title": "Second", "options": ["Yes"]}]})])
    snapshot = reader.snapshot(info())
    assert snapshot["pending"][QUESTION["id"]] == QUESTION
    assert list(snapshot["pending"])[-2:] == [question_id("call-2", 0), question_id("call-2", 1)]
    assert snapshot["pending"][question_id("call-2", 0)]["options"] == []


def test_unambiguous_workspace_fallback_and_subagent_exclusion(source):
    reader, _, db = source
    with pytest.raises(QuestionUnavailable):
        reader.snapshot(info(False))
    assert reader.snapshot(info(False), unique_workspace=True)["pending"]
    db.execute("INSERT INTO threads VALUES ('child','missing','/project',0,'/root/child')")
    db.commit()
    assert reader.snapshot(info(False), unique_workspace=True)["pending"]
    db.execute("INSERT INTO threads VALUES ('another','missing','/project',0,NULL)")
    db.commit()
    with pytest.raises(QuestionUnavailable, match="ambiguous"):
        reader.snapshot(info(False), unique_workspace=True)
    assert reader.snapshot(info())["pending"]


def test_response_record_resolves_exact_question_and_replay_cannot_resurrect(source):
    reader, path, _ = source
    reader.snapshot(info())
    append(path, [user_reply(), question_event()])
    snapshot = reader.snapshot(info())
    assert not snapshot["pending"] and snapshot["answers"][QUESTION["id"]] == "Purple"
    assert not CodexQuestionSource(reader.home).snapshot(info())["pending"]


@pytest.mark.parametrize("end", ["task_complete", "turn_aborted"])
def test_turn_end_expires_questions_without_fabricating_an_answer(source, end):
    reader, path, _ = source
    append(path, [event(end)])
    snapshot = reader.snapshot(info())
    assert not snapshot["pending"] and not snapshot["answers"]


def test_incomplete_append_is_not_consumed(source):
    reader, path, _ = source
    reader.snapshot(info())
    line = json.dumps(user_reply())
    with path.open("a") as stream:
        stream.write(line[:30])
    assert reader.snapshot(info())["pending"]
    with path.open("a") as stream:
        stream.write(line[30:] + "\n")
    assert not reader.snapshot(info())["pending"]


def test_file_replacement_and_wrong_session_fail_closed(source):
    reader, path, _ = source
    reader.snapshot(info())
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "another", "cwd": "/project"}}) + "\n")
    with pytest.raises(QuestionUnavailable, match="identity"):
        reader.snapshot(info())


def test_prose_tool_outputs_and_assistant_reply_envelopes_are_not_question_evidence(source):
    reader, path, _ = source
    append(path, [event("task_complete"), event("task_started"),
                  event("item_completed", item={"type": "AgentMessage", "id": "ordinary", "questions": [{"title": "x"}]}),
                  {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                      {"type": "input_text", "text": reply_text(QUESTION, "Purple")}]} }])
    snapshot = reader.snapshot(info())
    assert not snapshot["pending"] and not snapshot["answers"]


def test_prompt_plan_uses_native_reply_envelope_and_exact_pending_count():
    plan = input_plan(COLLAPSED, QUESTION, 1, "Green", [QUESTION["id"]])
    assert plan["mode"] == "prompt" and plan["keys"] == []
    assert json.loads(plan["text"].split("\n")[1])[0] == {
        "questionItemId": QUESTION["id"], "question": QUESTION["title"], "answer": "Green"}
    with pytest.raises(QuestionUnavailable):
        input_plan(COLLAPSED, QUESTION, 1, "Green", [QUESTION["id"], "another"])


@pytest.mark.parametrize("screen", [
    COLLAPSED.replace("› Ask Codex to do anything", "› unfinished local draft"),
    COLLAPSED.replace("? 1 question · 15s", "ordinary queued text"),
    COLLAPSED + "\nWould you like to run the following command?",
    EXPANDED.replace("Purple", "Destructive option"),
    EXPANDED.replace("1 of 1", "2 of 2"),
])
def test_input_guard_rejects_drafts_missing_or_changed_menus(screen):
    with pytest.raises(QuestionUnavailable):
        input_plan(screen, QUESTION, 0, "Purple", [QUESTION["id"]])


def test_expanded_exact_menu_uses_one_digit_and_cannot_guess_custom_answer():
    assert input_plan(EXPANDED, QUESTION, 1, "Green", [QUESTION["id"]]) == {
        "mode": "keys", "keys": ["2"], "answer": "Green"}
    with pytest.raises(QuestionUnavailable):
        input_plan(EXPANDED, QUESTION, None, "my text", [QUESTION["id"]])


class QuestionSource:
    def __init__(self):
        self.pending = {QUESTION["id"]: copy.deepcopy(QUESTION)}
        self.answers = {}

    def snapshot(self, info, **kwargs):
        return {"thread_id": "session-a", "turn_id": "turn-a", "pending": copy.deepcopy(self.pending),
                "answers": dict(self.answers)}


@pytest.fixture
def bridge(tmp_path):
    client, source = Client(), QuestionSource()
    client.info.update(cwd="/project", agent_status="working")
    client.screen = COLLAPSED
    bridge = Bridge(client, tmp_path / "bridge.sqlite", question_source=source,
                    confirmation_timeout=0.02, confirmation_interval=0.001)
    yield client, source, bridge
    bridge.close()


def request(bridge):
    client, _, core = bridge
    core.discover(client.info)
    return next(r["id"] for r in core.pending() if r.get("kind") == "codex_question")


def accept_prompts(client, source):
    def submit(pane, text):
        client.prompts.append((pane, text))
        reply = json.loads(text.split("\n")[1])[0]
        source.answers[reply["questionItemId"]] = reply["answer"]
        source.pending.pop(reply["questionItemId"], None)
    client.prompt_agent = submit
    client.submit_pane = submit


def test_working_question_discovery_never_writes_and_survives_sequence_changes(bridge):
    client, source, core = bridge
    token = request(bridge)
    client.info["state_change_seq"] += 4
    core.discover(client.info)
    assert [r["id"] for r in core.pending()] == [token]
    assert len(core.events()) == 1 and core.events()[0]["type"] == "question.requested"
    assert not client.keys and not client.prompts
    accept_prompts(client, source)
    result = core.answer(token, 1)
    assert result["outcome"] == "confirmed" and result["verification"]["reason"] == "question_answered"
    assert client.info["agent_status"] == "working"  # No blocked -> working transition required.
    assert len(client.prompts) == 1 and not client.keys
    assert core.answer(token, 1) == result
    with pytest.raises(BridgeError, match="different option"):
        core.answer(token, 0)


def test_question_revalidates_between_progress_and_write(bridge):
    client, source, core = bridge
    token = request(bridge)
    prepared = core.prepare_answer(token, option=0)
    source.pending.clear()  # Answered on PC while Slack progress was being edited.
    with pytest.raises(BridgeError, match="answered, expired or changed"):
        core.dispatch_reserved(token, prepared["attempt_id"])
    assert not client.prompts and not client.keys


def test_other_approval_and_local_draft_do_not_receive_question_reply(bridge):
    client, _, core = bridge
    token = request(bridge)
    client.info["agent_status"] = "blocked"
    client.screen = "Would you like to run the following command?\n› 1. Yes"
    with pytest.raises(BridgeError, match="approval is open"):
        core.answer(token, 0)
    client.info["agent_status"] = "working"
    client.screen = COLLAPSED.replace("› Ask Codex to do anything", "› local draft")
    with pytest.raises(BridgeError, match="draft"):
        core.answer(token, 0)
    assert not client.prompts and not client.keys


def test_custom_answer_is_explicit_idempotent_and_recorded(bridge):
    client, source, core = bridge
    token = request(bridge)
    accept_prompts(client, source)
    result = core.answer(token, answer_text="custom\nanswer")
    assert result["outcome"] == "confirmed" and result["option"] == "custom\nanswer"
    assert core.answer(token, answer_text="custom\nanswer") == result
    with pytest.raises(BridgeError):
        core.answer(token, answer_text="changed")
    assert len(client.prompts) == 1


def test_expiry_or_different_answer_is_not_confirmation(bridge):
    client, source, core = bridge
    token = request(bridge)
    def different(pane, text):
        client.prompts.append((pane, text))
        source.pending.clear()
        source.answers[QUESTION["id"]] = "Green"
    client.prompt_agent = different
    result = core.answer(token, 0)
    assert result["outcome"] == "uncertain" and result["verification"]["reason"] == "answered_differently"
    assert result["retry_token"] is None


def test_open_question_menu_does_not_create_an_unknown_approval_card(bridge):
    client, source, core = bridge
    client.screen, client.info["agent_status"] = EXPANDED, "blocked"
    token = request(bridge)
    assert [e["type"] for e in core.events()] == ["question.requested"]
    def accept(pane, keys):
        client.keys.append((pane, keys))
        source.pending.clear()
        source.answers[QUESTION["id"]] = "Green"
    client.send_keys = accept
    assert core.answer(token, 1)["outcome"] == "confirmed"
    assert client.keys == [("w4:p2", ["2"])] and not client.prompts


def test_restart_resumes_observation_without_replaying_question_answer(bridge, tmp_path):
    client, source, core = bridge
    token = request(bridge)
    result = core.answer(token, 0)
    assert result["outcome"] == "unconfirmed"
    source.pending.clear()
    source.answers[QUESTION["id"]] = "Purple"
    again = Bridge(client, tmp_path / "bridge.sqlite", question_source=source, confirmation_interval=0.001)
    try:
        again.reconcile_answers()
        assert again.result_cards(token)["result"]["outcome"] == "confirmed"
        assert len(client.prompts) == 1
    finally:
        again.close()


def test_turn_end_retires_pending_question_delivery(bridge):
    _, source, core = bridge
    request(bridge)
    source.pending.clear()
    assert not core.pending() and not core.events()


@pytest.mark.parametrize("screen", [
    COLLAPSED + "\n› actual bottom draft",
    COLLAPSED.replace("› Ask Codex to do anything\n", "› Ask Codex to do anything\n  wrapped local draft\n"),
])
def test_empty_old_prompt_cannot_hide_current_draft(screen):
    with pytest.raises(QuestionUnavailable):
        input_plan(screen, QUESTION, 0, "Purple", [QUESTION["id"]])


def test_pending_question_does_not_suppress_an_unrelated_unknown_blocked_dialog(bridge):
    client, _, core = bridge
    client.info["agent_status"], client.screen = "blocked", "A new unknown local dialog"
    core.discover(client.info)
    assert {e["type"] for e in core.events()} == {"question.requested", "agent.blocked"}


def test_explicit_question_retry_is_attempt_bound_and_never_automatic(bridge):
    client, source, core = bridge
    token = request(bridge)
    first = core.answer(token, answer_text="custom answer")
    assert first["outcome"] == "unconfirmed" and first["retry_token"]
    core.reconcile_answers()
    assert len(client.prompts) == 1
    accept_prompts(client, source)
    second = core.retry(token, first["retry_token"])
    assert second["outcome"] == "confirmed" and second["attempt_id"] != first["attempt_id"]
    assert core.retry(token, first["retry_token"])["outcome"] == "confirmed"
    assert len(client.prompts) == 2


def test_freeform_question_with_no_suggestions_uses_exact_reply(bridge):
    client, source, core = bridge
    source.pending[QUESTION["id"]]["options"] = []
    token = request(bridge)
    assert core.pending()[0]["options"] == []
    accept_prompts(client, source)
    assert core.answer(token, answer_text="a freeform answer")["outcome"] == "confirmed"
    assert not client.keys


def test_pending_question_disappearance_and_read_failure_do_not_confirm(bridge):
    client, source, core = bridge
    token = request(bridge)
    def expire(pane, text):
        client.prompts.append((pane, text))
        source.pending.clear()
    client.prompt_agent = expire
    result = core.answer(token, 0)
    assert result["outcome"] == "uncertain" and result["verification"]["reason"] == "question_expired"
    source.snapshot = lambda *a, **kw: (_ for _ in ()).throw(QuestionUnavailable("unreadable"))
    assert core.check(token)["outcome"] == "uncertain"
    assert len(client.prompts) == 1


def test_reserved_question_restart_never_dispatches_on_scan(bridge, tmp_path):
    client, source, core = bridge
    token = request(bridge)
    core.prepare_answer(token, option=0)
    again = Bridge(client, tmp_path / "bridge.sqlite", question_source=source, confirmation_interval=0.001)
    try:
        again.reconcile_answers()
        assert again.result_cards(token)["result"]["outcome"] == "not_sent"
        assert not client.prompts and not client.keys
    finally:
        again.close()


def test_collapsed_question_misclassified_as_blocked_uses_one_encoded_submission(bridge):
    client, source, core = bridge
    client.info["agent_status"] = "blocked"
    token = request(bridge)
    assert [e["type"] for e in core.events()] == ["question.requested"]
    accept_prompts(client, source)
    client.prompt_agent = lambda *a: pytest.fail("Herdr rejects agent.prompt for blocked input")
    prepared = core.prepare_answer(token, option=1)
    assert not client.prompts
    result = core.dispatch_reserved(token, prepared["attempt_id"])
    assert result["outcome"] == "confirmed" and len(client.prompts) == 1
    assert core.answer(token, 1)["outcome"] == "confirmed" and len(client.prompts) == 1


@pytest.mark.parametrize("suffix", ["\n› local draft", "\n› Ask Codex to do anything", "\nWould you like to run the following command?"])
def test_historical_question_menu_cannot_authorize_keys_into_a_different_input(suffix):
    with pytest.raises(QuestionUnavailable):
        input_plan(EXPANDED + suffix, QUESTION, 0, "Purple", [QUESTION["id"]])
