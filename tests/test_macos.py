"""macOS portability and real Unix socket framing/shutdown regressions."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib

import pytest

from herdr_slackbot import herdr_client as hc, plugin, procinfo


@contextmanager
def unix_peer(handler):
    if not hasattr(socket, "AF_UNIX") or sys.platform == "win32":
        pytest.skip("Unix-domain sockets required")
    # Darwin limits sockaddr_un paths; pytest's full test name can exceed that limit.
    with tempfile.TemporaryDirectory(prefix="slackbot-", dir="/tmp") as directory:
        path = str(Path(directory) / "herdr.sock")
        errors = queue.Queue()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen()
            listener.settimeout(3)

            def serve():
                try:
                    with listener.accept()[0] as connection:
                        connection.settimeout(3)
                        handler(connection)
                except BaseException as exc:
                    errors.put(exc)

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield path
            finally:
                worker.join(4)
                assert not worker.is_alive(), "test peer did not shut down"
                if not errors.empty():
                    raise errors.get()


def receive_request(connection):
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(65536)
        assert chunk
        data.extend(chunk)
    return json.loads(data)


def test_unix_rpc_preserves_ndjson_and_unicode():
    seen = []

    def reply(connection):
        request = receive_request(connection)
        seen.append(request)
        payload = json.dumps({"id": request["id"], "result": {"text": "你好"}}, ensure_ascii=False).encode() + b"\n"
        # Partial headers and even a split UTF-8 codepoint must be reconstructed.
        for byte in payload:
            connection.sendall(bytes([byte]))

    with unix_peer(reply) as path:
        assert hc.UnixSocketTransport(path).request("agent.prompt", {"text": "hi\n你好"}) == {"text": "你好"}
    assert seen[0]["method"] == "agent.prompt" and seen[0]["params"]["text"] == "hi\n你好"


def test_unix_subscription_keeps_ack_leftovers_and_cancels_blocked_read():
    reader_waiting = threading.Event()

    def subscribe(connection):
        assert receive_request(connection)["method"] == "events.subscribe"
        connection.sendall(b'{"result":{"type":"subscription_started"}}\n{"event":"first"}\n')
        # Await client shutdown instead of closing the stream prematurely.
        assert connection.recv(1) == b""

    with unix_peer(subscribe) as path:
        stream = hc.UnixSocketTransport(path).open_stream("events.subscribe", {"subscriptions": []})
        assert stream.ack == {"type": "subscription_started"}
        events = []

        def consume():
            for event in stream:
                events.append(event)
                reader_waiting.set()

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        assert reader_waiting.wait(2)
        stream.close()
        reader.join(1)
        assert not reader.is_alive()
        assert events == [{"event": "first"}]
        stream.close()  # idempotent


def test_unix_lost_mutation_reply_is_never_resent(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Unix-domain sockets required")
    monkeypatch.setattr(hc.sys, "platform", "darwin")
    submitted = []

    def drop_reply(connection):
        submitted.append(receive_request(connection))

    with unix_peer(drop_reply) as path:
        auto = hc.AutoTransport(path)
        monkeypatch.setattr(auto.cli, "request", lambda *_: pytest.fail("mutation retried through CLI"))
        with pytest.raises(hc.HerdrOutcomeUnknown):
            auto.request("agent.prompt", {"target": "a", "text": "once"})
    assert len(submitted) == 1


def test_native_transport_selection(monkeypatch):
    monkeypatch.setattr(hc.sys, "platform", "win32")
    assert isinstance(hc.AutoTransport("named-pipe").pipe, hc.PipeTransport)
    monkeypatch.setattr(hc.sys, "platform", "darwin")
    assert isinstance(hc.AutoTransport("/tmp/herdr.sock").pipe, hc.UnixSocketTransport)


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS process identity")
def test_macos_process_identity_and_children():
    own = procinfo.current_start_time()
    assert own is not None and own == procinfo.process_start_time(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        start = procinfo.process_start_time(child.pid)
        assert start is not None and start >= own
        assert child.pid in {pid for pid, _ in procinfo.child_processes(os.getpid())}
        assert child.pid in {pid for pid, _ in procinfo.busy_children(os.getpid())}
    finally:
        child.communicate(timeout=3)
    assert procinfo.process_start_time(child.pid) is None


def test_macos_config_and_login_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin.sys, "platform", "darwin")
    assert plugin.default_config_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "herdr/plugins/config/herdr-slackbot"
    assert plugin.shell_kind("/bin/zsh") == "zsh"
    assert plugin.shell_kind("-zsh") == "zsh"
    # A literal POSIX backslash must survive the command typed into a shell.
    root = str(tmp_path / "it's a \\ project")
    cmd = plugin.bridge_command("zsh", sys.executable, root, str(tmp_path))
    assert "\\ project" in cmd and "'\\''" in cmd
    assert plugin.bridge_command("", sys.executable, root, str(tmp_path)) == cmd
    assert plugin.bridge_command("unrecognized", sys.executable, root, str(tmp_path)) == cmd


def test_macos_manifest_has_unique_actions_and_native_commands():
    root = Path(__file__).parent.parent
    manifest = tomllib.loads((root / "legacy/herdr-plugin.toml").read_text())
    ids = [a["id"] for a in manifest["actions"]]
    assert len(set(ids)) == len(ids)
    assert "macos" in manifest["platforms"] and "windows" in manifest["platforms"]
    native = [a for a in manifest["actions"] if "macos" in a["platforms"]]
    assert {a["id"] for a in native} == {f"{name}-macos" for name in ("setup", "start", "restart", "stop", "status")}
    assert all(a["command"][0] == "sh" for a in native)
    if sys.platform != "win32":
        for script in ("setup.sh", "herdr-slackbot.sh"):
            subprocess.run(["sh", "-n", str(root / "scripts" / script)], check=True)


def test_macos_entrypoint_runs_from_another_directory(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS entrypoint")
    root = Path(__file__).parent.parent
    config = tmp_path / "config with spaces"
    config.mkdir()
    result = subprocess.run(["sh", str(root / "scripts/herdr-slackbot.sh"), "manifest", "--config-dir", str(config)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["settings"]["socket_mode_enabled"] is True
