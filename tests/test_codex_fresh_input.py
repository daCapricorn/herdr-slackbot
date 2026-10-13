"""Fresh native identity and receipt boundaries, without interacting with a TUI."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
import tempfile

import pytest

from herdr_bridge.core import Bridge, BridgeError
from herdr_bridge.codex_session import SessionUnavailable
from herdr_bridge import agent_adapters
from test_codex_title_identity import A, B, TWIN, Home, Client, agent, short, title, rows, meta, turn
from test_terminal_inputs import CODEX_EMPTY, ingest, ready_channel


def native_path(home, tid=A):
    return home / f"sessions/2026/10/12/rollout-2026-10-12T19-42-02-{tid}.jsonl"


class Native:
    def __init__(self, home, tid=A):
        self.ids = {tid}
        self.thread = {"id": tid, "cwd": "/project", "source": "vscode", "parentThreadId": None,
                       "ephemeral": False, "status": {"type": "idle"}, "canAcceptDirectInput": True,
                       "path": str(native_path(home, tid))}
        self.reads = 0
        self.race = None

    def loaded_ids(self):
        return set(self.ids)

    def read(self, tid):
        self.reads += 1
        if self.race:
            self.race(self)
        return copy.deepcopy(self.thread)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    native = Native(home.path)
    route = {"pid": 100, "started": 1.0, "executable": "/bin/codex", "home": str(home.path.resolve())}
    monkeypatch.setattr(agent_adapters, "daemon_available", lambda home: True, raising=False)
    monkeypatch.setattr(agent_adapters, "foreground_codex_route", lambda *args: route, raising=False)
    @contextmanager
    def observer(home):
        yield native
    monkeypatch.setattr(agent_adapters, "observe_daemon", observer, raising=False)
    raw = {**agent(terminal="term-1", session=B), "state_change_seq": 1}
    client = Client(raw)
    client.list_panes = lambda: client.list_agents()
    client.screen = CODEX_EMPTY
    client.read_agent_once = lambda *args, **kwargs: client.screen
    client.prompts = []
    def prompt(pane, text):
        client.prompts.append((pane, text))
        path = native_path(home.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rows(meta(A, "/project"), *turn(1, text)))
        home.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", (A, str(path), "/project", 0, None, "gpt-db"))
        home.db.commit()
        client.agents[pane].update(agent_status="working", state_change_seq=2)
        native.thread["status"] = {"type": "active"}
    client.prompt_agent = prompt
    bridge = Bridge(client, tmp_path / "bridge.sqlite", confirmation_timeout=0, notification_delay=0)
    ready_channel(bridge)
    yield home, native, client, bridge
    bridge.close()
    home.db.close()


def test_fresh_first_prompt_receipt_and_assistant_readback(fresh):
    home, native, client, bridge = fresh
    path = native_path(home.path)
    assert not path.exists() and not home.db.execute("SELECT * FROM threads").fetchall()
    result = ingest(bridge, "100.1", "first Slack task")
    assert result["outcome"] == "confirmed"
    rec = bridge.inputs.find("slack", "default", "C1", "100.1")
    assert rec["session_id"] == A
    assert rec["receipt_baseline"]["missing"] is True
    assert rec["receipt_baseline"]["path"] == str(path)
    assert rec["receipt_baseline"]["offset"] == 0
    assert client.prompts == [("w1:p1", "first Slack task")]
    assert ingest(bridge, "100.1")["outcome"] == "confirmed"
    assert len(client.prompts) == 1
    bridge.sync_conversation(client.get_agent("w1:p1"))
    with path.open("a") as stream:
        stream.write(rows({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "answer-1", "phase": "final_answer", "text": "FRESH_ASSISTANT_MARKER"}}}))
    bridge.sync_conversation(client.get_agent("w1:p1"))
    events = [json.loads(row[0]) for row in bridge.db.execute("SELECT event FROM outbox")]
    assert any(event.get("text") == "FRESH_ASSISTANT_MARKER" for event in events)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("materialized_before_baseline", [False, True])
def test_fast_first_turn_completed_before_discovery_is_mirrored_once(fresh, restart, materialized_before_baseline):
    home, native, client, bridge = fresh
    if materialized_before_baseline:
        path = native_path(home.path)
        path.parent.mkdir(parents=True)
        path.write_text(rows(meta(A, "/project")))
    original = client.prompt_agent
    def fast(pane, text):
        original(pane, text)
        with native_path(home.path).open("a") as stream:
            stream.write(rows({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "AgentMessage", "id": "fast-answer", "phase": "final_answer", "text": "FAST_FIRST_ANSWER"}}}))
        client.agents[pane].update(agent_status="idle", state_change_seq=3)
        native.thread["status"] = {"type": "idle"}
    client.prompt_agent = fast
    assert ingest(bridge, "100.1")["outcome"] == "confirmed"
    reader = bridge
    if restart:
        reader = Bridge(client, home.path.parent / "bridge.sqlite",
                        confirmation_timeout=0, notification_delay=0)
    try:
        reader.sync_conversation(client.get_agent("w1:p1"))
        reader.sync_conversation(client.get_agent("w1:p1"))
        events = [json.loads(row[0]) for row in reader.db.execute("SELECT event FROM outbox")]
        assert [event.get("text") for event in events].count("FAST_FIRST_ANSWER") == 1
        assert [event.get("text") for event in events].count("do the task") == 1
        assert len(client.prompts) == 1
    finally:
        if reader is not bridge:
            reader.close()


@pytest.mark.parametrize("archived,child,cwd", [(1, None, "/project"), (0, "child", "/project"), (0, None, "/elsewhere")])
def test_collision_counts_all_persisted_rows_before_filtering(fresh, archived, child, cwd):
    home, native, client, bridge = fresh
    home.thread(TWIN, cwd, archived=archived, agent_path=child)
    assert "ambiguous" in bridge._agent("w1:p1")["authoritative_session_error"]
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert client.prompts == []
    assert not bridge.db.execute("SELECT * FROM terminal_inputs").fetchall()


def test_loaded_prefix_collision_rejects_even_when_candidate_would_be_invalid(fresh):
    _, native, client, bridge = fresh
    native.ids.add(TWIN)
    assert "ambiguous" in bridge._agent("w1:p1")["authoritative_session_error"]
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


def test_same_uuid_across_loaded_and_persisted_inventory_is_deduplicated(fresh):
    home, native, client, bridge = fresh
    path = native_path(home.path)
    path.parent.mkdir(parents=True)
    path.write_text(rows(meta(A, "/project")))
    home.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", (A, str(path), "/project", 0, None, "model"))
    home.db.commit()
    info = bridge._agent("w1:p1")
    assert info["agent_session"]["value"] == A and "codex_live_identity" not in info


def test_persisted_no_daemon_remains_supported_when_socket_exists(fresh):
    home, _, client, bridge = fresh
    home.thread(A)
    client.processes["w1:p1"]["foreground_processes"][0]["argv"] += ["--no-daemon"]
    assert bridge._agent("w1:p1")["agent_session"]["value"] == A


def test_fresh_no_daemon_remains_unsupported(fresh):
    _, _, client, bridge = fresh
    client.processes["w1:p1"]["foreground_processes"][0]["argv"] += ["--no-daemon"]
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


@pytest.mark.parametrize("override", ["--config=remote='unix://'", "-cexec_server_url='ws://elsewhere'", "-c=codex_home='/elsewhere'"])
def test_config_route_overrides_fail_closed(fresh, override):
    _, _, client, bridge = fresh
    client.processes["w1:p1"]["foreground_processes"][0]["argv"] += [override]
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


@pytest.mark.parametrize("change", [
    {"id": B}, {"cwd": "/elsewhere"}, {"source": "exec"}, {"source": {"subAgent": {}}},
    {"parentThreadId": B}, {"ephemeral": True}, {"canAcceptDirectInput": None},
    {"status": {"type": "notLoaded"}}, {"path": "/tmp/rollout.jsonl"}, {"path": None},
])
def test_malformed_or_unsupported_live_metadata_never_writes(fresh, change):
    _, native, client, bridge = fresh
    native.thread.update(change)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts and not bridge.db.execute("SELECT * FROM terminal_inputs").fetchall()


@pytest.mark.parametrize("remove", ["parentThreadId", "canAcceptDirectInput", "ephemeral", "status", "path"])
def test_missing_live_metadata_fields_fail_closed(fresh, remove):
    _, native, client, bridge = fresh
    native.thread.pop(remove)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


@pytest.mark.parametrize("change", ["title", "terminal", "cwd", "pid", "config", "membership", "metadata"])
def test_read_races_reject_without_resurrecting_stale_session(fresh, change):
    home, native, client, bridge = fresh
    def race(observer):
        if observer.reads != 1:
            return
        if change == "title":
            client.agents["w1:p1"]["terminal_title_stripped"] = title(short(B))
        elif change == "terminal":
            client.agents["w1:p1"]["terminal_id"] = "replacement"
        elif change == "cwd":
            client.agents["w1:p1"]["cwd"] = "/elsewhere"
        elif change == "pid":
            client.processes["w1:p1"]["foreground_processes"][0]["pid"] += 1
        elif change == "config":
            (home.path / "config.toml").write_text('[tui]\nterminal_title=["thread"]\n')
        elif change == "membership":
            observer.ids = {B}
        else:
            observer.thread["path"] = str(native_path(home.path, B))
    native.race = race
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


@pytest.mark.parametrize("body", ["broken json\n", rows(meta(B, "/project")), rows(meta(A, "/elsewhere"))])
def test_existing_corrupt_rollout_never_gets_missing_baseline(fresh, body):
    home, _, client, bridge = fresh
    path = native_path(home.path)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


def test_unreadable_existing_rollout_never_gets_missing_baseline(fresh, monkeypatch):
    home, _, client, bridge = fresh
    path = native_path(home.path)
    path.parent.mkdir(parents=True)
    path.write_text(rows(meta(A, "/project")))
    original = Path.open
    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("denied")
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


def test_file_appearing_during_baseline_is_validated_existing_file(fresh):
    home, _, _, bridge = fresh
    info = bridge._agent("w1:p1")
    path = native_path(home.path)
    path.parent.mkdir(parents=True)
    path.write_text(rows(meta(A, "/project")))
    baseline = bridge.adapters["codex"].source.receipt_baseline(info)
    assert "missing" not in baseline and baseline["offset"] == path.stat().st_size


def test_materialization_race_at_first_stat_is_reread(fresh, monkeypatch):
    home, _, _, bridge = fresh
    info = bridge._agent("w1:p1")
    path = native_path(home.path)
    original = Path.stat
    triggered = []
    def race(candidate, *args, **kwargs):
        if candidate == path and not triggered:
            triggered.append(True)
            path.parent.mkdir(parents=True)
            path.write_text(rows(meta(A, "/project")))
            raise FileNotFoundError()
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", race)
    baseline = bridge.adapters["codex"].source.receipt_baseline(info)
    assert "missing" not in baseline and baseline["offset"] == path.stat().st_size


def test_inode_replacement_during_baseline_validation_is_rejected(fresh, monkeypatch):
    from herdr_bridge import codex_sync
    home, _, _, bridge = fresh
    info = bridge._agent("w1:p1")
    path = native_path(home.path)
    path.parent.mkdir(parents=True)
    path.write_text(rows(meta(A, "/project")))
    original = codex_sync.validate_rollout
    def replace(session):
        result = original(session)
        replacement = path.with_suffix(".tmp")
        replacement.write_text(rows(meta(A, "/project")))
        replacement.replace(path)
        return result
    monkeypatch.setattr(codex_sync, "validate_rollout", replace)
    with pytest.raises(SessionUnavailable, match="changed"):
        bridge.adapters["codex"].source.receipt_baseline(info)


def test_fresh_baseline_failure_rejects_before_send(fresh, monkeypatch):
    _, _, client, bridge = fresh
    def denied(info):
        raise PermissionError("denied")
    monkeypatch.setattr(bridge.adapters["codex"].source, "receipt_baseline", denied)
    result = ingest(bridge, "100.1")
    assert result["outcome"] == "rejected" and result["reason"] == "guard_unavailable"
    assert not client.prompts


def test_title_switch_after_baseline_before_reserve_rejects(fresh, monkeypatch):
    home, native, client, bridge = fresh
    source = bridge.adapters["codex"].source
    original = source.receipt_baseline
    def switched(info):
        result = original(info)
        replacement = Native(home.path, B)
        native.ids, native.thread = replacement.ids, replacement.thread
        client.agents["w1:p1"]["terminal_title_stripped"] = title(short(B))
        return result
    monkeypatch.setattr(source, "receipt_baseline", switched)
    assert ingest(bridge, "100.1")["outcome"] == "rejected"
    assert not client.prompts


def test_native_path_switch_after_baseline_before_reserve_rejects(fresh, monkeypatch):
    _, native, client, bridge = fresh
    source = bridge.adapters["codex"].source
    original = source.receipt_baseline
    def switched(info):
        result = original(info)
        native.thread["path"] = native.thread["path"].replace("T19-42-02-", "T19-42-03-")
        return result
    monkeypatch.setattr(source, "receipt_baseline", switched)
    assert ingest(bridge, "100.1")["outcome"] == "rejected"
    assert not client.prompts


@pytest.mark.parametrize("layer", ["user", "system", "project", "requirements"])
def test_relocated_sqlite_home_cannot_hide_persisted_collisions(fresh, tmp_path, monkeypatch, layer):
    home, _, client, bridge = fresh
    elsewhere = tmp_path / "sqlite-home"
    elsewhere.mkdir()
    db = sqlite3.connect(elsewhere / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, agent_path TEXT)")
    db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (TWIN, "/irrelevant", "/elsewhere", 1, None))
    db.commit()
    db.close()
    if layer == "user":
        path = home.path / "config.toml"
        path.write_text("sqlite_home=" + json.dumps(str(elsewhere)) + "\n" + path.read_text())
    elif layer == "project":
        cwd = tmp_path / "project"
        cwd.mkdir()
        client.agents["w1:p1"]["cwd"] = str(cwd)
        path = cwd / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text("sqlite_home=" + json.dumps(str(elsewhere)))
    else:
        path = tmp_path / (layer + ".toml")
        path.write_text("sqlite_home=" + json.dumps(str(elsewhere)))
        monkeypatch.setattr(agent_adapters, "SYSTEM_CONFIG_PATH" if layer == "system" else "SYSTEM_REQUIREMENTS_PATH", path)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


@pytest.mark.parametrize("condition", ["draft", "blocked", "working", "unknown"])
def test_fresh_composer_and_status_guards(fresh, condition):
    _, _, client, bridge = fresh
    if condition == "draft":
        client.screen = CODEX_EMPTY.replace("Ask Codex to do anything", "local draft").replace("\x1b[2m", "")
    else:
        client.agents["w1:p1"]["agent_status"] = {"blocked": "blocked", "working": "working", "unknown": None}[condition]
    result = ingest(bridge, "100.1")
    assert result["outcome"] == ("queued" if condition == "working" else "rejected")
    assert not client.prompts


def test_new_fresh_session_ignores_saved_binding_and_old_native_metadata(fresh):
    home, native, client, bridge = fresh
    home.thread(B)
    bridge.adapters["codex"].source.bind({**client.get_agent("w1:p1"), "terminal_title_stripped": title(B)}, B)
    assert bridge._agent("w1:p1")["agent_session"]["value"] == A
    assert bridge.db.execute("SELECT thread_id FROM codex_session_bindings").fetchall() == [(B,)]


def test_wrong_path_or_session_receipt_cannot_confirm(fresh):
    home, _, client, bridge = fresh
    info = bridge._agent("w1:p1")
    source = bridge.adapters["codex"].source
    baseline = source.receipt_baseline(info)
    client.prompt_agent("w1:p1", "expected")
    live = bridge._agent("w1:p1")
    assert source.receipt_recorded(live, baseline, "expected")
    assert not source.receipt_recorded(live, {**baseline, "path": str(native_path(home.path, B))}, "expected")
    assert not source.receipt_recorded(live, {**baseline, "source_key": "term-1:" + B}, "expected")
    assert not source.receipt_recorded(live, baseline, "different")


def test_corrupt_database_does_not_become_a_fresh_identity(fresh):
    home, _, client, bridge = fresh
    home.db.execute("DROP TABLE threads")
    home.db.commit()
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


def test_unreadable_database_inventory_is_not_empty(fresh, monkeypatch):
    home, _, client, bridge = fresh
    original = Path.iterdir
    def denied(candidate):
        if candidate == home.path:
            raise PermissionError("denied")
        return original(candidate)
    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts


def test_no_state_database_yet_is_the_narrow_fresh_home_case(fresh):
    home, _, _, bridge = fresh
    home.db.close()
    (home.path / "state_5.sqlite").unlink()
    assert bridge._agent("w1:p1")["agent_session"]["value"] == A


def test_native_path_rejects_date_mismatch_traversal_and_escape_symlinks(fresh, tmp_path):
    from herdr_bridge.codex_session import native_rollout_path
    home, _, _, _ = fresh
    path = native_path(home.path)
    for value in (str(path).replace("/2026/10/12/", "/2026/10/13/"), str(path.parent / ".." / path.name)):
        with pytest.raises(SessionUnavailable):
            native_rollout_path(value, A, home.path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home.path / "sessions").symlink_to(target)
    with pytest.raises(SessionUnavailable):
        native_rollout_path(str(path), A, home.path)


class FakeWebSocket:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.sent = []
    def send(self, data):
        self.sent.append(json.loads(data))
    def recv(self, timeout):
        return json.dumps(next(self.responses))


@pytest.mark.parametrize("pages", [
    [{"data": [A]}], [{"data": [A], "nextCursor": "invalid"}],
    [{"data": [], "nextCursor": A}], [{"data": [A, A], "nextCursor": None}],
    [{"data": [A], "nextCursor": A}, {"data": [A], "nextCursor": None}],
    [{"data": ["bad-id"], "nextCursor": None}],
])
def test_incomplete_or_invalid_native_inventory_rejects(pages):
    from herdr_bridge.codex_daemon import DaemonObserver
    ws = FakeWebSocket(*({"id": i + 1, "result": page} for i, page in enumerate(pages)))
    with pytest.raises(SessionUnavailable):
        DaemonObserver(ws, time.monotonic() + 1).loaded_ids()


def test_metadata_only_allowlist_and_complete_pagination():
    from herdr_bridge.codex_daemon import DaemonObserver
    ws = FakeWebSocket({"method": "notification", "params": {}},
        {"id": 1, "result": {"data": [B], "nextCursor": B}},
        {"id": 2, "result": {"data": [A], "nextCursor": None}})
    observer = DaemonObserver(ws, time.monotonic() + 1)
    assert observer.loaded_ids() == {A, B}
    for method, params in (("thread/start", {}), ("thread/read", {"threadId": A, "includeTurns": True})):
        with pytest.raises(SessionUnavailable):
            observer.request(method, params)
    assert [row["method"] for row in ws.sent] == ["thread/loaded/list", "thread/loaded/list"]


def test_observer_socket_failure_and_deadline_are_bounded(tmp_path):
    from herdr_bridge.codex_daemon import observe_daemon, socket_path
    with pytest.raises(SessionUnavailable):
        with observe_daemon(tmp_path, timeout=0.1):
            pytest.fail("missing socket")
    short_home = tempfile.TemporaryDirectory(prefix="hcu-", dir="/tmp")
    path = socket_path(short_home.name)
    path.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    accepted = []
    def stall():
        connection, _ = listener.accept()
        accepted.append(connection)
        while connection.recv(4096):
            pass
        connection.close()
    worker = threading.Thread(target=stall, daemon=True)
    worker.start()
    start = time.monotonic()
    with pytest.raises(SessionUnavailable):
        with observe_daemon(short_home.name, timeout=0.1):
            pytest.fail("stalled handshake")
    assert time.monotonic() - start < 0.8
    worker.join(timeout=1)
    listener.close()
    short_home.cleanup()
    assert accepted and not worker.is_alive()


@pytest.mark.parametrize("failure", ["socket", "timeout", "inventory"])
def test_daemon_errors_never_fall_back_to_saved_identity(fresh, monkeypatch, failure):
    home, native, client, bridge = fresh
    home.thread(B)
    if failure == "inventory":
        native.loaded_ids = lambda: (_ for _ in ()).throw(SessionUnavailable("incomplete inventory"))
    else:
        @contextmanager
        def failed(home):
            raise TimeoutError() if failure == "timeout" else OSError()
            yield
        monkeypatch.setattr(agent_adapters, "observe_daemon", failed)
    with pytest.raises(BridgeError):
        ingest(bridge, "100.1")
    assert not client.prompts and not bridge.db.execute("SELECT * FROM terminal_inputs").fetchall()


@pytest.mark.parametrize("change", ["home", "remote_env", "exec_env", "pid", "argv", "cwd", "exe", "node", "remote_flag", "no_daemon",
    "oss", "strict-config", "dangerously-bypass-hook-trust", "approve-for-me", "search", "enable", "disable", "config",
    "workload-rule", "workload-token", "sqlite-env"])
def test_foreground_native_route_is_exact(tmp_path, monkeypatch, change):
    # Restore the actual helper (not the fresh fixture's injected proof).
    from herdr_bridge.agent_adapters import foreground_codex_route
    import psutil
    home = tmp_path / "codex"
    raw = agent()
    process = Client(raw).process_info("w1:p1")
    values = {"started": 1, "exe": "/native/codex", "argv": ["codex"], "cwd": "/project", "pid_gone": False,
              "env": {"CODEX_HOME": str(home), "HOME": str(tmp_path), "UNRELATED_SECRET": "never retained"}}
    class Process:
        def __init__(self, pid):
            if values["pid_gone"]:
                raise psutil.NoSuchProcess(pid)
        def create_time(self): return values["started"]
        def exe(self): return values["exe"]
        def cmdline(self): return values["argv"]
        def cwd(self): return values["cwd"]
        def environ(self): return dict(values["env"])
    monkeypatch.setattr(psutil, "Process", Process)
    assert foreground_codex_route(raw, process, home)["home"] == str(home.resolve())
    if change == "home": values["env"]["CODEX_HOME"] = str(tmp_path / "other")
    elif change == "pid": values["pid_gone"] = True
    elif change == "remote_env": values["env"]["CODEX_APP_SERVER_URL"] = "ws://elsewhere"
    elif change == "exec_env": values["env"]["CODEX_EXEC_SERVER_URL"] = "ws://elsewhere"
    elif change == "sqlite-env": values["env"]["CODEX_SQLITE_HOME"] = str(tmp_path / "elsewhere")
    elif change == "workload-rule": values["env"]["OPENAI_FEDERATION_RULE_ID"] = ""
    elif change == "workload-token": values["env"]["OPENAI_IDENTITY_TOKEN_FILE"] = ""
    elif change == "argv": values["argv"] = ["codex", "new"]
    elif change == "cwd": values["cwd"] = "/elsewhere"
    elif change == "exe": values["exe"] = "/native/python"
    elif change == "node": process["foreground_processes"][0]["argv"] = ["node", "codex.js"]
    elif change in ("remote_flag", "no_daemon"):
        process["foreground_processes"][0]["argv"] += ["--remote=unix://" if change == "remote_flag" else "--no-daemon"]
    else:
        process["foreground_processes"][0]["argv"] += ["--" + change]
    with pytest.raises((SessionUnavailable, psutil.NoSuchProcess)):
        foreground_codex_route(raw, process, home)
