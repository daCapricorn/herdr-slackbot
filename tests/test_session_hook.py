import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest
from herdr_bridge.session_hook import report_session
from herdr_bridge.codex_session import SessionUnavailable
from herdr_bridge.codex_sync import CodexRolloutSync

from test_codex_title_identity import A, B, Client as TitleClient, Home, agent, append, enrich, short, title, turn


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


# Default shared daemon: hooks descend from the daemon (DAEMON), never from a
# pane's TUI (pids 100+), and inherit a stale pane marker.
DAEMON = {90067}
DAEMON_ENV = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/test/socket", "HERDR_PANE_ID": "w9:p9"}


class DaemonClient(TitleClient):
    def __init__(self, *agents, argv=("codex",)):
        super().__init__(*agents, argv=argv)
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))


def prompt(tid, turn_id="t9"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": tid, "cwd": "/project",
            "turn_id": turn_id, "transcript_path": None}


def start(tid, source):
    return {"hook_event_name": "SessionStart", "source": source, "session_id": tid,
            "cwd": "/project", "transcript_path": None}


def observations(home):
    return sorted(p.name for p in (home.path / "herdr-bridge/sessions").glob("*.json"))


def answer(n, text):
    return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": f"t{n}",
            "item": {"type": "AgentMessage", "id": f"a{n}", "phase": "final_answer", "text": text}}}


@pytest.mark.parametrize("payload,shown", [
    (prompt(A, "t2"), A),            # first prompt after /resume <A>
    (start(B, "clear"), B),          # /new: the title already names the new thread
    (start(B, "startup"), B),
])
def test_default_daemon_title_hook_cutoff_keeps_turn_completed_before_first_scan(tmp_path, monkeypatch, payload, shown):
    home = Home(tmp_path, monkeypatch)
    paths = {A: home.thread(A, body=turn(1, "old A")), B: home.thread(B, body=turn(1, "old B"))}
    # Herdr's native identity is stale/null; only the title names the shown thread.
    client = DaemonClient(agent(text=title(short(shown)), session=A if shown == B else None))
    assert report_session(payload, DAEMON_ENV, client, home=home.path, seq=42, pids=DAEMON)
    assert client.calls[0][1]["pane_id"] == "w1:p1" and client.calls[0][1]["agent_session_id"] == shown
    assert observations(home) == ["term-w1-p1.json"]
    # The whole turn finishes before the worker's first 5-second scan of an idle pane.
    append(paths[shown], *turn(2, "fast prompt"), answer(2, "fast answer"))
    db = sqlite3.connect(":memory:")
    source = CodexRolloutSync(db, home=home.path)
    messages = []
    info = enrich(client)
    assert info["agent_status"] == "idle" and info["agent_session"]["value"] == shown
    source.sync(info, lambda eid, agent, message: messages.append(message))
    source.sync(info, lambda *args: pytest.fail("duplicate"))
    assert [(m["role"], m["text"]) for m in messages] == [("user", "fast prompt"), ("assistant", "fast answer")]
    assert not db.execute("SELECT * FROM codex_session_bindings").fetchall()  # title ids never become bindings


@pytest.mark.parametrize("shown", [
    title(short(A)),       # /new before the title updates: still names the previous thread
    "Codex | /project",    # /new before the new thread exists: no id
])
def test_default_daemon_new_session_hook_rejects_stale_or_missing_title(tmp_path, monkeypatch, shown):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = DaemonClient(agent(text=shown, session=B))  # even Herdr's native agrees with the payload
    with pytest.raises(SessionUnavailable):
        report_session(start(B, "clear"), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []


def test_default_daemon_same_directory_panes_report_their_own_threads(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = DaemonClient(agent("w1:p1", title(short(A))), agent("w1:p2", title(B)))
    assert report_session(prompt(A), DAEMON_ENV, client, home=home.path, seq=1, pids=DAEMON)
    assert report_session(prompt(B), DAEMON_ENV, client, home=home.path, seq=2, pids=DAEMON)
    assert [(c[1]["pane_id"], c[1]["agent_session_id"]) for c in client.calls] == [("w1:p1", A), ("w1:p2", B)]


def test_default_daemon_duplicate_same_thread_panes_are_ambiguous(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    client = DaemonClient(agent("w1:p1", title(short(A))), agent("w1:p2", title(A)))
    with pytest.raises(SessionUnavailable):
        report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []


def test_no_daemon_ancestor_still_wins_over_duplicate_titles(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    client = DaemonClient(agent("w1:p1", title(short(A))), agent("w1:p2", title(short(A))))
    client.processes["w1:p2"]["foreground_processes"][0]["argv"] = ["codex", "--no-daemon"]
    assert report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids={101})
    assert client.calls[0][1]["pane_id"] == "w1:p2"


def test_default_daemon_ignores_stale_resume_argv_and_native_session(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    # Started as `codex resume A`, then switched to B in the same process.
    client = DaemonClient(agent(text=title(short(B)), session=A), argv=("codex", "resume", A))
    with pytest.raises(SessionUnavailable):
        report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == []
    assert report_session(prompt(B), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls[0][1]["agent_session_id"] == B


def test_default_daemon_without_title_export_never_accepts_native_session(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, config=None)
    home.thread(A)
    client = DaemonClient(agent(text=title(short(A)), session=A))
    with pytest.raises(SessionUnavailable, match="foreground ancestor"):
        report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []


CHILD = "01a12091-da21-7800-a5b6-c0ffee000001"


@pytest.mark.parametrize("shown,payload_tid,env", [
    (short(A), CHILD, {}),                     # subagent hook while the root is shown
    (CHILD, CHILD, {}),                        # a title naming a child thread is not a root identity
    (short(A), A, {"CODEX_THREAD_ID": B}),     # nested agent with an inherited, different thread
])
def test_default_daemon_nested_and_child_threads_never_report(tmp_path, monkeypatch, shown, payload_tid, env):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    home.thread(CHILD, agent_path="explorer")
    client = DaemonClient(agent(text=title(shown)))
    try:
        reported = report_session(prompt(payload_tid), {**DAEMON_ENV, **env}, client, home=home.path, pids=DAEMON)
    except SessionUnavailable:
        reported = False
    assert not reported and client.calls == [] and observations(home) == []


@pytest.mark.parametrize("change", [
    lambda c: c.agents["w1:p1"].update(terminal_title_stripped=title(short(B))),
    lambda c: c.agents["w1:p1"].update(terminal_title_stripped="Codex | /project"),
    lambda c: c.agents["w1:p1"].update(terminal_id="term-new"),
    lambda c: c.agents["w1:p1"].update(cwd="/elsewhere"),
    lambda c: c.processes["w1:p1"]["foreground_processes"][0].update(pid=999),
])
def test_default_daemon_title_is_revalidated_before_saving_cutoff(tmp_path, monkeypatch, change):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = DaemonClient(agent(text=title(short(A))))
    import herdr_bridge.session_hook as module
    resolve = module.resolve_session

    def racing(*args, **kwargs):
        result = resolve(*args, **kwargs)
        change(client)  # after selection, before the final check
        return result
    monkeypatch.setattr(module, "resolve_session", racing)
    assert not report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []


@pytest.mark.parametrize("change", [
    lambda c: c.agents["w1:p1"].update(terminal_title_stripped=title(short(B))),
    lambda c: c.agents["w1:p1"].update(terminal_id="term-new"),
    lambda c: c.processes["w1:p1"]["foreground_processes"][0].update(pid=999),
    lambda c: c.agents["w1:p2"].update(terminal_title_stripped=title(short(A))),
])
def test_default_daemon_mutation_during_cutoff_scan_never_saves_or_reports(tmp_path, monkeypatch, change):
    home = Home(tmp_path, monkeypatch)
    path = home.thread(A, body=turn(9, "prompt"))
    home.thread(B)
    client = DaemonClient(agent(text=title(short(A))), agent("w1:p2", title(short(B))))
    original = Path.open

    class RacingStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def readline(self, *args):
            line = self.stream.readline(*args)
            if not args:  # cutoff scan; header validation has an explicit limit
                change(client)
            return line

    def racing_open(opened, *args, **kwargs):
        stream = original(opened, *args, **kwargs)
        return RacingStream(stream) if opened == path and args == ("rb",) else stream
    monkeypatch.setattr(Path, "open", racing_open)
    assert not report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []
    assert not (home.path / "herdr-bridge/sessions").exists()


@pytest.mark.parametrize("argv", [
    ("codex", "--profile", "work"), ("codex", "-c", "tui={notifications=true}"),
])
def test_default_daemon_unsupported_title_config_never_witnesses_hook(tmp_path, monkeypatch, argv):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    client = DaemonClient(agent(session=A), argv=argv)
    with pytest.raises(SessionUnavailable, match="unique foreground"):
        report_session(prompt(A), DAEMON_ENV, client, home=home.path, pids=DAEMON)
    assert client.calls == [] and observations(home) == []
