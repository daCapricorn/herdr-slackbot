import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest
from herdr_bridge.session_hook import report_session
from herdr_bridge.codex_session import SessionUnavailable
from herdr_bridge.codex_sync import CodexRolloutSync


TID = "01a12091-da21-7800-a5b6-b02fff2d0af0"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_bridge.session_hook.caller_pids", lambda: {123})
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": TID, "cwd": "/project"}}) + "\n")
    db = sqlite3.connect(tmp_path / "state_1.sqlite")
    db.execute("CREATE TABLE threads (id,rollout_path,cwd,archived,agent_path)")
    db.execute("INSERT INTO threads VALUES (?,?,?,0,NULL)", (TID, str(rollout), "/project"))
    db.commit()
    class Client:
        info = {"pane_id": "w1:p1", "terminal_id": "term-a", "agent": "codex", "cwd": "/project"}
        calls = []
        def get_agent(self, pane):
            return self.info.copy()
        def list_agents(self):
            return [self.info.copy()]
        def process_info(self, pane):
            return {"pane_id": pane, "foreground_processes": [{"pid": 123, "argv": ["codex", "--no-daemon"]}]}
        def request(self, method, params):
            self.calls.append((method, params))
    env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": "/test/socket"}
    payload = {"hook_event_name": "SessionStart", "source": "clear", "session_id": TID,
               "cwd": "/project", "transcript_path": None}
    yield Client(), env, payload, tmp_path, db
    db.close()


def test_exact_root_session_reports_with_null_transcript_path(hook):
    client, env, payload, home, _ = hook
    assert report_session(payload, env, client, home=home, seq=42)
    assert client.calls == [("pane.report_agent_session", {"pane_id": "w1:p1", "source": "herdr:codex",
        "agent": "codex", "seq": 42, "agent_session_id": TID, "session_start_source": "clear"})]


def test_prompt_hook_refreshes_identity_without_session_start_side_effects(hook):
    client, env, payload, home, _ = hook
    assert report_session({**payload, "hook_event_name": "UserPromptSubmit"}, env, client, home=home)
    assert "session_start_source" not in client.calls[0][1]


@pytest.mark.parametrize("changes", [{"HERDR_ENV": "0"},
    {"HERDR_SOCKET_PATH": ""}, {"CODEX_THREAD_ID": "parent-thread"}])
def test_invalid_environment_and_nested_agents_do_not_report(hook, changes):
    client, env, payload, home, _ = hook
    assert not report_session(payload, {**env, **changes}, client, home=home)
    assert client.calls == []


@pytest.mark.parametrize("changes", [{"hook_event_name": "Stop"}, {"session_id": "invalid"},
    {"source": "unknown"}])
def test_unrelated_or_invalid_payload_does_not_report(hook, changes):
    client, env, payload, home, _ = hook
    assert not report_session({**payload, **changes}, env, client, home=home)
    assert client.calls == []


def test_subagent_database_record_is_not_a_pane_identity(hook):
    client, env, payload, home, db = hook
    db.execute("UPDATE threads SET agent_path='subagent'")
    db.commit()
    with pytest.raises(SessionUnavailable):
        report_session(payload, env, client, home=home)
    assert client.calls == []


def test_replaced_terminal_during_validation_does_not_report(hook):
    client, env, payload, home, _ = hook
    results = iter([client.info.copy(), {**client.info, "terminal_id": "term-b"}])
    client.get_agent = lambda pane: next(results)
    assert not report_session(payload, env, client, home=home)
    assert client.calls == []


def test_installer_preserves_existing_hooks_and_trust_and_is_idempotent(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/install-codex-session-hook.py"
    spec = importlib.util.spec_from_file_location("session_hook_installer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]}}
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(old))
    config = tmp_path / "config.toml"
    config.write_text('trusted_hash = "unchanged"\n')
    assert module.install(tmp_path, python="/python path", project="/repo path")["changed"]
    current = json.loads(path.read_text())
    assert current["hooks"]["SessionStart"][0] == old["hooks"]["SessionStart"][0]
    assert "UserPromptSubmit" in current["hooks"]
    assert not module.install(tmp_path, python="/python path", project="/repo path")["changed"]
    assert json.loads(path.with_name("hooks.json.herdr-bridge-backup").read_text()) == old
    assert config.read_text() == 'trusted_hash = "unchanged"\n'


def test_hook_cutoff_syncs_a_turn_completed_before_first_poll_without_old_history(hook):
    client, env, payload, home, _ = hook
    path = home / "rollout.jsonl"
    with path.open("a") as file:
        file.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "historical"}}) + "\n")
    assert report_session(payload, env, client, home=home, seq=42)
    with path.open("a") as file:
        for event in [{"type": "user_message", "message": "new prompt"},
                      {"type": "item_completed", "item": {"type": "AgentMessage", "phase": "final_answer", "text": "new answer"}}]:
            file.write(json.dumps({"type": "event_msg", "payload": event}) + "\n")
    db = sqlite3.connect(home / "bridge.sqlite")
    source = CodexRolloutSync(db, home=home)
    messages = []
    info = {**client.info, "agent_status": "done", "agent_session": {"value": TID}}
    source.sync(info, lambda eid, agent, message: messages.append(message))
    source.sync(info, lambda *args: pytest.fail("duplicate"))
    assert [(m["role"], m["text"]) for m in messages] == [("user", "new prompt"), ("assistant", "new answer")]


def test_out_of_order_hook_cannot_replace_a_newer_observation(hook):
    client, env, payload, home, _ = hook
    assert report_session(payload, env, client, home=home, seq=42)
    assert not report_session(payload, env, client, home=home, seq=41)
    assert len(client.calls) == 1


def test_moved_pane_uses_exact_foreground_ancestor_not_stale_environment(hook):
    client, env, payload, home, _ = hook
    assert report_session(payload, {**env, "HERDR_PANE_ID": "w9:p9"}, client, home=home)
    assert client.calls[0][1]["pane_id"] == "w1:p1"


def test_shared_daemon_cannot_guess_a_pane_even_with_matching_environment(hook):
    client, env, payload, home, _ = hook
    with pytest.raises(SessionUnavailable, match="foreground ancestor"):
        report_session(payload, env, client, home=home, pids={456})
    assert client.calls == []


def test_changed_directory_cannot_report_into_the_original_pane(hook):
    client, env, payload, home, _ = hook
    with pytest.raises(SessionUnavailable):
        report_session({**payload, "cwd": "/other"}, env, client, home=home)
    assert client.calls == []


def test_same_directory_multiple_panes_match_only_the_hook_ancestor(hook):
    client, env, payload, home, _ = hook
    client.list_agents = lambda: [client.info.copy(), {**client.info, "pane_id": "w1:p2", "terminal_id": "term-b"}]
    client.process_info = lambda pane: {"pane_id": pane, "foreground_processes": [{"pid": 123 if pane == "w1:p1" else 456,
        "argv": ["codex", "--no-daemon"]}]}
    assert report_session(payload, env, client, home=home)
    assert client.calls[0][1]["pane_id"] == "w1:p1"


def test_shared_daemon_is_rejected_even_if_first_tui_is_an_ancestor(hook):
    client, env, payload, home, _ = hook
    client.process_info = lambda pane: {"pane_id": pane, "foreground_processes": [{"pid": 123, "argv": ["codex"]}]}
    with pytest.raises(SessionUnavailable):
        report_session(payload, env, client, home=home)
    assert client.calls == []


def test_prompt_hook_uses_exact_turn_cutoff_not_last_historical_turn(hook):
    client, env, payload, home, _ = hook
    path = home / "rollout.jsonl"
    with path.open("a") as file:
        for event in [{"type": "task_started", "turn_id": "old"},
                      {"type": "user_message", "message": "historical"},
                      {"type": "task_started", "turn_id": "current"},
                      {"type": "user_message", "message": "current prompt"}]:
            file.write(json.dumps({"type": "event_msg", "payload": event}) + "\n")
    assert report_session({**payload, "hook_event_name": "UserPromptSubmit", "turn_id": "current"},
                          env, client, home=home, seq=42)
    db = sqlite3.connect(home / "bridge.sqlite")
    source = CodexRolloutSync(db, home=home)
    messages = []
    source.sync({**client.info, "agent_status": "done", "agent_session": {"value": TID}},
                lambda eid, agent, message: messages.append(message))
    assert [m["text"] for m in messages] == ["current prompt"]
