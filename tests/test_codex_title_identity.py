"""Shared-daemon Codex panes: the native terminal-title export is the exact client identity."""
import json
import sqlite3

import pytest

from herdr_bridge.agent_adapters import ClaudeAdapter, adapters
from herdr_bridge.core import Bridge

A = "01a12091-da21-7800-a5b6-b02fff2d0af0"
B = "019e0000-1111-7222-8333-444455556666"
TWIN = "01a12091-da21-7800-a5b6-b02ff0000001"  # same first 29 characters as A
EXPORT = '["session-id", "thread", "current-dir"]'


def short(tid):
    return tid[:29] + "..."


def title(prefix, thread="Fix the bug", cwd="/project"):
    return f"{prefix} | {thread} | {cwd}"


def rows(*values):
    return "".join(json.dumps(v) + "\n" for v in values)


def meta(tid, cwd):
    return {"type": "session_meta", "payload": {"id": tid, "cwd": cwd}}


def turn(n, text, model="gpt-old"):
    return [{"type": "turn_context", "payload": {"model": model}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": f"t{n}"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": f"t{n}",
             "item": {"type": "UserMessage", "id": f"u{n}", "content": [{"type": "text", "text": text}]}}}]


def question(n):
    return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": f"t{n}", "item": {
        "type": "AgentMessage", "id": f"q{n}", "delivery": "async",
        "questions": [{"title": "Which branch?", "options": ["main", "dev"]}]}}}


class Home:
    def __init__(self, tmp_path, monkeypatch, config=f"[tui]\nterminal_title = {EXPORT}\n"):
        self.path = tmp_path / "codex"
        self.path.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(self.path))
        if config is not None:
            (self.path / "config.toml").write_text('model = "gpt-old"\n' + config)
        self.db = sqlite3.connect(self.path / "state_5.sqlite")
        self.db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, agent_path TEXT, model TEXT)")

    def thread(self, tid, cwd="/project", *, archived=0, agent_path=None, header=None, body=()):
        path = self.path / f"rollout-{tid}.jsonl"
        path.write_text(rows(header or meta(tid, cwd), *body))
        self.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", (tid, str(path), cwd, archived, agent_path, "gpt-db"))
        self.db.commit()
        return path


class Client:
    """Herdr agent/process reads; `race` mutates state after the first process read."""
    def __init__(self, *agents, argv=("codex",)):
        self.agents = {a["pane_id"]: a for a in agents}
        self.processes = {p: {"pane_id": p, "foreground_processes": [{"pid": 100 + i, "argv": list(argv)}]}
                          for i, p in enumerate(self.agents)}
        self.race = None

    def get_agent(self, pane):
        return json.loads(json.dumps(self.agents[pane]))

    def list_agents(self):
        return [json.loads(json.dumps(a)) for a in self.agents.values()]

    def list_tabs(self, workspace):
        return [{"tab_id": "w1:t1", "label": "codex-wa"}]

    def read_agent_once(self, *args, **kwargs):
        return ""

    def process_info(self, pane):
        result = json.loads(json.dumps(self.processes[pane]))
        if self.race:
            race, self.race = self.race, None
            race(self)
        return result


def agent(pane="w1:p1", text=None, *, session=None, cwd="/project", terminal=None):
    return {"agent": "codex", "pane_id": pane, "terminal_id": terminal or "term-" + pane.replace(":", "-"),
            "cwd": cwd, "agent_status": "idle", "workspace_id": "w1", "tab_id": "w1:t1",
            "terminal_title_stripped": text if text is not None else title(short(A)),
            "agent_session": {"value": session, "source": "herdr:codex"} if session else None}


def enrich(client, pane="w1:p1"):
    db = sqlite3.connect(":memory:")
    adapter = adapters(db)["codex"]
    return adapter.enrich(client.get_agent(pane), client)


def test_registry_uses_codex_adapter_and_preserves_claude_adapter(tmp_path, monkeypatch):
    Home(tmp_path, monkeypatch)
    registry = adapters(sqlite3.connect(":memory:"))
    assert type(registry["codex"]).__name__ == "CodexAdapter"
    assert isinstance(registry["claude"], ClaudeAdapter)


@pytest.mark.parametrize("prefix", [short(A), A])
def test_default_daemon_title_export_resolves_exact_root_session(tmp_path, monkeypatch, prefix):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    info = enrich(Client(agent(text=title(prefix))))
    assert info["agent_session"] == {"agent": "codex", "kind": "id", "source": "bridge:codex-title", "value": A}
    assert "authoritative_session_error" not in info


def test_same_directory_panes_keep_distinct_title_sessions(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = Client(agent("w1:p1", title(short(A))), agent("w1:p2", title(short(B))))
    assert enrich(client, "w1:p1")["agent_session"]["value"] == A
    assert enrich(client, "w1:p2")["agent_session"]["value"] == B


@pytest.mark.parametrize("twin_cwd", ["/project", "/elsewhere"])
def test_truncated_prefix_shared_by_another_thread_is_ambiguous(tmp_path, monkeypatch, twin_cwd):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(TWIN, twin_cwd)
    info = enrich(Client(agent(session=A)))
    assert info["agent_session"] is None
    assert "ambiguous" in info["authoritative_session_error"]
    # The complete identifier is still exact.
    assert enrich(Client(agent(text=title(A))))["agent_session"]["value"] == A


SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # Codex TERMINAL_TITLE_SPINNER_FRAMES


@pytest.mark.parametrize("text", [
    short(A),                                          # helper-valid one-item export: no delimiter
    A,
    *[title(f"{short(A)} {frame}") for frame in SPINNERS],  # title generation pending
    f"{A} ⠏ | Fix the bug",
    f"{short(A)} ⠋",
])
def test_sole_item_and_pending_title_spinner_keep_exact_identifier(tmp_path, monkeypatch, text):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    assert enrich(Client(agent(text=text)))["agent_session"]["value"] == A


@pytest.mark.parametrize("text", [
    title(short(A)[:-4] + "..."),                      # too short
    title(A[:30] + "..."),                             # neither 29+... nor 36
    title(short(A).upper()),
    "Working on " + title(short(A)),                   # prose before the identifier
    title("Fix the bug", thread=short(A)),             # identifier not in the first item
    "Fix the bug | /project " + A,                     # trailing identifier
    short(A) + " Fix the bug",                         # wrong delimiter
    "⠋ " + title(short(A)),                            # unsupported activity prefix
    "● " + title(short(A)),                            # microphone prefix
    "[ ! ] Action Required | " + title(short(A)),      # blocked activity prefix
    title(short(A) + "⠋"),                             # spinner without its space
    title(short(A) + " ⠋⠙"),                           # more than one frame
    title(short(A) + "  ⠋"),
    title(short(A) + " ●"),                            # not a spinner frame
    title(short(A) + " ⠋ Working"),
    short(A) + " ",
    "",
])
def test_title_without_exact_leading_identifier_fails_closed(tmp_path, monkeypatch, text):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    info = enrich(Client(agent(text=text, session=A)))
    assert info["agent_session"] is None
    assert info["authoritative_session_error"]


@pytest.mark.parametrize("kwargs", [
    {"agent_path": "explorer"},
    {"archived": 1},
    {"cwd": "/elsewhere"},
    {"header": meta(B, "/project")},
    {"header": meta(A, "/elsewhere")},
])
def test_title_candidate_must_be_current_root_thread_with_matching_rollout(tmp_path, monkeypatch, kwargs):
    home = Home(tmp_path, monkeypatch)
    home.thread(A, **kwargs)
    info = enrich(Client(agent(session=A)))
    assert info["agent_session"] is None
    assert info["authoritative_session_error"]


def test_truncated_title_candidate_must_be_a_full_hex_thread_id(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    bad = A[:29] + "zzzzzzz"  # 36 characters, same prefix, matching rollout header
    home.thread(bad)
    info = enrich(Client(agent()))
    assert info["agent_session"] is None
    assert "valid thread id" in info["authoritative_session_error"]


def test_unknown_title_session_is_unavailable(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(B)
    info = enrich(Client(agent()))
    assert info["agent_session"] is None
    assert "not found" in info["authoritative_session_error"]


def test_resume_in_same_process_follows_title_not_stale_argv(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = Client(agent(text=title(short(B)), session=A), argv=("codex", "resume", A))
    assert enrich(client)["agent_session"]["value"] == B
    client.agents["w1:p1"]["terminal_title_stripped"] = title(short(A))
    assert enrich(client)["agent_session"]["value"] == A


@pytest.mark.parametrize("change", [
    lambda c: c.agents["w1:p1"].update(terminal_title_stripped=title(short(B))),
    lambda c: c.agents["w1:p1"].update(terminal_id="term-new"),
    lambda c: c.agents["w1:p1"].update(cwd="/elsewhere"),
    lambda c: c.agents["w1:p1"].update(agent="claude"),
    lambda c: c.processes["w1:p1"]["foreground_processes"][0].update(pid=999),
])
def test_identity_changes_while_reading_title_fail_closed(tmp_path, monkeypatch, change):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    home.thread(B)
    client = Client(agent(session=A))
    client.race = change
    info = enrich(client)
    assert info["agent_session"] is None
    assert "changed" in info["authoritative_session_error"]


def test_foreground_process_must_be_codex_for_title_identity(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    info = enrich(Client(agent(session=A), argv=("vim", "notes.md")))
    assert info["agent_session"] is None
    assert info["authoritative_session_error"]


@pytest.mark.parametrize("config,argv,enabled", [
    (f"[tui]\nterminal_title = {EXPORT}\n", ("codex",), True),
    (f"[tui]\nterminal_title = {EXPORT}\n", ("codex", "-c", 'tui.terminal_title=["thread","current-dir"]'), False),
    (None, ("codex", "--config", 'tui.terminal_title=["session-id","thread"]', "resume", B), True),
    ("[tui]\nterminal_title = [\"thread\"]\n", ("codex", "--config=tui.terminal_title=[\"thread-id\"]"), True),
    (None, ("codex", "-c", 'tui.terminal_title=["session-id"]', "-c", 'tui.terminal_title=["thread"]'), False),
    ("[tui]\nterminal_title = [\"thread\", \"session-id\"]\n", ("codex",), False),
    (None, ("codex",), False),
])
def test_config_file_and_foreground_override_precedence(tmp_path, monkeypatch, config, argv, enabled):
    home = Home(tmp_path, monkeypatch, config=config)
    home.thread(A)
    home.thread(B)
    info = enrich(Client(agent(session=B), argv=argv))
    if enabled:
        assert info["agent_session"]["value"] == A
    else:
        # Unconfigured export keeps the existing native/manual contract unchanged.
        assert info == Client(agent(session=B)).get_agent("w1:p1")


@pytest.mark.parametrize("config,override,enabled", [
    ('[tui]\nterminal_title = ["thread"]\n', 'tui.terminal_title=["session-id"]', True),
    (f"[tui]\nterminal_title = {EXPORT}\n", 'tui.terminal_title=["thread"]', False),
])
@pytest.mark.parametrize("option", ["-c=", "-c", "--config="])
def test_attached_title_override_precedence(tmp_path, monkeypatch, config, override, enabled, option):
    home = Home(tmp_path, monkeypatch, config=config)
    home.thread(A)
    home.thread(B)
    client = Client(agent(session=B), argv=("codex", option + override))
    info = enrich(client)
    if enabled:
        assert info["agent_session"]["value"] == A
        assert info["agent_session"]["source"] == "bridge:codex-title"
    else:
        assert info == client.get_agent("w1:p1")


@pytest.mark.parametrize("override,error", [
    ('tui={terminal_title=["session-id"]}', "unsupported"),
    ('tui.terminal_title.first="session-id"', "unsupported"),
    ('profile="work"', "profile"),
    ('profiles={work={}}', "profile"),
    ('profiles.work.tui.terminal_title=["session-id"]', "profile"),
])
def test_short_config_equals_unsupported_override_fails_closed(tmp_path, monkeypatch, override, error):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    info = enrich(Client(agent(session=B), argv=("codex", "-c=" + override)))
    assert info["agent_session"] is None
    assert error in info["authoritative_session_error"]


def test_malformed_config_is_not_treated_as_disabled(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, config="[tui\nterminal_title = [\n")
    home.thread(A)
    info = enrich(Client(agent(session=A)))
    assert info["agent_session"] is None
    assert "config" in info["authoritative_session_error"]


@pytest.mark.parametrize("overrides", [
    ('tui={notifications=true}',),
    ('tui.terminal_title=["session-id"]', 'tui={notifications=true}'),
])
def test_top_level_tui_override_is_explicitly_unsupported(tmp_path, monkeypatch, overrides):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    argv = ["codex"]
    for override in overrides:
        argv.extend(["-c", override])
    info = enrich(Client(agent(session=B), argv=argv))
    assert info["agent_session"] is None
    assert "unsupported" in info["authoritative_session_error"]


@pytest.mark.parametrize("argv", [
    ("codex", "--profile", "work"), ("codex", "--profile=work"),
    ("codex", "-p", "work"), ("codex", "-pwork"),
    ("codex", "-c", 'profile="work"'),
])
def test_selected_profile_cannot_claim_global_title_config(tmp_path, monkeypatch, argv):
    home = Home(tmp_path, monkeypatch, config=None)
    home.thread(A)
    info = enrich(Client(agent(session=B), argv=argv))
    assert info["agent_session"] is None
    assert "profile" in info["authoritative_session_error"]


@pytest.mark.parametrize("mode", ["exception", "missing", "wrong-pane", "not-codex"])
def test_disabled_global_export_requires_an_observed_codex_foreground(tmp_path, monkeypatch, mode):
    Home(tmp_path, monkeypatch, config=None)
    client = Client(agent(session=B), argv=("codex", "-c", 'tui.terminal_title=["session-id"]'))
    if mode == "exception":
        def unavailable(pane):
            raise OSError("process unavailable")
        client.process_info = unavailable
    elif mode == "missing":
        client.processes["w1:p1"]["foreground_processes"] = []
    elif mode == "wrong-pane":
        client.processes["w1:p1"]["pane_id"] = "w1:p2"
    else:
        client.processes["w1:p1"]["foreground_processes"][0]["argv"] = ["vim"]
    info = enrich(client)
    assert info["agent_session"] is None
    assert info["authoritative_session_error"]


@pytest.mark.parametrize("global_config", [None, f"[tui]\nterminal_title = {EXPORT}\n"])
@pytest.mark.parametrize("project_config", [
    '[tui]\nterminal_title = ["thread"]\n', '[tui]\nterminal_title = ["session-id"]\n',
    '[tui\n',
])
def test_ancestor_title_override_or_invalid_config_is_unsupported(tmp_path, monkeypatch, global_config, project_config):
    home = Home(tmp_path, monkeypatch, config=global_config)
    cwd = tmp_path / "project" / "nested"
    cwd.mkdir(parents=True)
    config_dir = cwd.parent / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(project_config)
    home.thread(A, cwd=str(cwd))
    info = enrich(Client(agent(cwd=str(cwd), session=B)))
    assert info["agent_session"] is None
    assert "config" in info["authoritative_session_error"]


def test_unreadable_ancestor_config_is_not_ignored(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, config=None)
    config_path = tmp_path / ".codex/config.toml"
    original = type(home.path).read_text
    def unreadable(path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(home.path), "read_text", unreadable)
    info = enrich(Client(agent(cwd=str(tmp_path), session=B)))
    assert info["agent_session"] is None
    assert "config" in info["authoritative_session_error"]


@pytest.mark.parametrize("forced", [True, None])
def test_managed_preferences_present_or_unknown_fail_closed(tmp_path, monkeypatch, forced):
    import herdr_bridge.agent_adapters as module
    Home(tmp_path, monkeypatch, config=None)
    def managed():
        if forced is None:
            raise OSError("preferences unavailable")
        return forced
    monkeypatch.setattr(module, "forced_codex_preferences", managed, raising=False)
    info = enrich(Client(agent(session=B)))
    assert info["agent_session"] is None
    assert "managed" in info["authoritative_session_error"]


def test_managed_config_file_is_explicitly_unsupported(tmp_path, monkeypatch):
    import herdr_bridge.agent_adapters as module
    Home(tmp_path, monkeypatch, config=None)
    path = tmp_path / "managed_config.toml"
    path.write_text('[tui]\nterminal_title=["session-id"]\n')
    monkeypatch.setattr(module, "MANAGED_CONFIG_PATH", path, raising=False)
    info = enrich(Client(agent(session=B)))
    assert info["agent_session"] is None
    assert "managed" in info["authoritative_session_error"]


@pytest.mark.parametrize("new_title", [title(short(A) + " ⠙"), title(short(A), thread="Renamed thread")])
def test_cosmetic_title_change_keeps_same_session_prefix(tmp_path, monkeypatch, new_title):
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    client = Client(agent(text=title(short(A) + " ⠋")))
    client.race = lambda c: c.agents["w1:p1"].update(terminal_title_stripped=new_title)
    assert enrich(client)["agent_session"]["value"] == A


def test_unrelated_ancestor_tui_settings_preserve_supported_export(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()
    directory = tmp_path / ".codex"
    directory.mkdir()
    (directory / "config.toml").write_text('[tui]\nnotifications=true\n')
    home.thread(A, cwd=str(cwd))
    assert enrich(Client(agent(cwd=str(cwd))))["agent_session"]["value"] == A


def test_codex_home_is_excluded_from_ancestor_title_layer_check(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    new_home = tmp_path / ".codex"
    home.path.rename(new_home)
    monkeypatch.setenv("CODEX_HOME", str(new_home))
    from herdr_bridge.agent_adapters import check_title_config_layers
    check_title_config_layers(new_home, str(tmp_path))


@pytest.mark.parametrize("config", ['profile="work"\n', '[tui]\nterminal_title="session-id"\n'])
def test_unmodeled_user_title_config_is_unavailable(tmp_path, monkeypatch, config):
    Home(tmp_path, monkeypatch, config=config)
    info = enrich(Client(agent(session=B)))
    assert info["agent_session"] is None and info["authoritative_session_error"]


def test_system_title_layer_is_explicitly_unsupported(tmp_path, monkeypatch):
    import herdr_bridge.agent_adapters as module
    Home(tmp_path, monkeypatch, config=None)
    path = tmp_path / "system-config.toml"
    path.write_text('[tui]\nterminal_title=["session-id"]\n')
    monkeypatch.setattr(module, "SYSTEM_CONFIG_PATH", path)
    info = enrich(Client(agent(session=B)))
    assert info["agent_session"] is None and "unsupported" in info["authoritative_session_error"]


def test_unsupported_title_config_blocks_native_manual_and_directory_fallback(tmp_path, monkeypatch):
    from herdr_bridge.codex_session import SessionUnavailable, resolve_session
    from herdr_bridge.codex_sync import CodexRolloutSync
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    raw = agent(session=A)
    client = Client(raw, argv=("codex", "--profile", "work"))
    info = enrich(client)
    source = CodexRolloutSync(sqlite3.connect(":memory:"), home=home.path)
    source.bind(raw, A)
    for kwargs in ({}, {"thread_id": A}, {"unique_workspace": True}):
        with pytest.raises(SessionUnavailable, match="profile"):
            resolve_session(info, home=home.path, **kwargs)
    with pytest.raises(SessionUnavailable, match="profile"):
        source.bind(info, A)
    assert source.sync(info, lambda *args: pytest.fail("unproved transcript"), unique_workspace=True) == 0
    assert source.diagnostic(info)["state"] == "unavailable"


def test_native_session_change_under_unknown_title_authority_cannot_dispatch_approval(tmp_path, monkeypatch):
    from pathlib import Path
    from herdr_bridge.core import BridgeError
    Home(tmp_path, monkeypatch, config=None)
    raw = {**agent(session=A), "agent_status": "blocked", "state_change_seq": 7}
    client = Client(raw)
    keys = []
    client.send_keys = lambda pane, value: keys.append((pane, value))
    screen = (Path(__file__).parent / "fixtures/transcripts/dialog_codex_approval.txt").read_text()
    client.read_agent_once = lambda *args, **kwargs: screen
    def unavailable(pane):
        raise OSError("foreground process read failed")
    client.process_info = unavailable
    bridge = Bridge(client, tmp_path / "bridge.sqlite", confirmation_timeout=0, notification_delay=0)
    try:
        bridge.discover(client.get_agent("w1:p1"))
        token = bridge.db.execute("SELECT id FROM approvals").fetchone()[0]
        client.agents["w1:p1"]["agent_session"] = {"value": B, "source": "herdr:codex"}
        with pytest.raises(BridgeError):
            bridge.answer(token, 0)
        assert keys == []
    finally:
        bridge.close()


def test_claude_enrichment_ignores_codex_title_export(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    home = Home(tmp_path, monkeypatch)
    home.thread(A)
    claude = {**agent(session=None), "agent": "claude"}
    client = Client(claude)
    info = adapters(sqlite3.connect(":memory:"))["claude"].enrich(client.get_agent("w1:p1"), client)
    assert info == claude


def bridge_for(tmp_path, client):
    bridge = Bridge(client, tmp_path / "bridge.sqlite")
    events = lambda: [json.loads(r[0]) for r in bridge.db.execute("SELECT event FROM outbox ORDER BY rowid")]
    return bridge, events


def append(path, *values):
    with path.open("a") as stream:
        stream.write(rows(*values))


def test_title_session_syncs_without_persisting_an_automatic_binding(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    path_a = home.thread(A, body=turn(1, "old"))
    path_b = home.thread(B, body=turn(1, "other"))
    client = Client(agent())
    bridge, events = bridge_for(tmp_path, client)
    bridge.sync_conversation(client.get_agent("w1:p1"))
    append(path_a, *turn(2, "for A"))
    append(path_b, *turn(2, "for B"))
    bridge.sync_conversation(client.get_agent("w1:p1"))
    assert [e["text"] for e in events()] == ["for A"]
    assert bridge.sync_status("w1:p1")["thread_id"] == A
    assert bridge.status()[0]["session"] == A
    assert bridge.status()[0]["name"] == "codex-wa"  # the tab label, never the UUID title
    assert not bridge.db.execute("SELECT * FROM codex_session_bindings").fetchall()
    bridge.close()


def test_new_session_without_title_id_ignores_manual_binding_native_session_and_cwd(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    path = home.thread(A, body=[*turn(1, "old"), question(1)])
    client = Client(agent(session=A))
    bridge, events = bridge_for(tmp_path, client)
    bridge.sync_conversation(client.get_agent("w1:p1"))
    bridge.bind_session("w1:p1", A)
    assert bridge.status()[0]["model"] == "gpt-old"
    assert bridge.discover_questions(bridge._agent("w1:p1"))
    # `/new`: the widget clears its thread id; Herdr still reports the old native id.
    client.agents["w1:p1"]["terminal_title_stripped"] = "Codex | /project"
    append(path, *turn(2, "must not be attributed"))
    assert bridge.sync_conversation(client.get_agent("w1:p1")) == 0
    assert "must not be attributed" not in json.dumps(events())
    status = bridge.sync_status("w1:p1")
    assert status["state"] == "unavailable" and "title" in status["reason"]
    public = bridge.status()[0]
    assert public["session"] is None and public["model"] is None
    assert bridge.discover_questions(bridge._agent("w1:p1")) == {}
    with pytest.raises(Exception):
        bridge.adapters["codex"].source.bound_info(bridge._agent("w1:p1"))
    # The saved manual association is not used as a fallback or rewritten.
    assert bridge.db.execute("SELECT thread_id FROM codex_session_bindings").fetchall() == [(A,)]
    bridge.close()


def test_export_failure_blocks_completion_claim_from_old_stream(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    home.thread(A, body=turn(1, "old"))
    client = Client(agent())
    bridge, _ = bridge_for(tmp_path, client)
    bridge.sync_conversation(client.get_agent("w1:p1"))
    assert bridge.sync_status("w1:p1")["state"] == "syncing"
    client.agents["w1:p1"]["terminal_title_stripped"] = "Codex | /project"
    failed = bridge._agent("w1:p1")
    source = bridge.adapters["codex"].source
    assert source.claim_completion(failed) is False
    with pytest.raises(Exception):
        source._resolve(failed, unique_workspace=True)
    bridge.close()
