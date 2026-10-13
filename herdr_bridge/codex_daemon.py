"""Bounded read-only observer of an already-running local Codex daemon.

Only initialize, loaded/list and metadata-only read are allowed. In the native
server these reads don't attach a listener; observer disconnect doesn't unload
threads owned by the TUI. This module never starts or repairs a daemon.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import stat
import threading
import time

from .codex_session import SessionUnavailable, THREAD_ID

TIMEOUT = 1.5
MAX_THREADS = 4096
PAGE_SIZE = 128
MAX_MESSAGES = 256


def socket_path(home):
    return Path(home) / "app-server-control/app-server-control.sock"


def daemon_available(home):
    try:
        mode = socket_path(home).stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionUnavailable("Cannot inspect Codex daemon socket") from exc
    if not stat.S_ISSOCK(mode):
        raise SessionUnavailable("Codex daemon endpoint is not a Unix socket")
    return True


class DaemonObserver:
    def __init__(self, websocket, deadline):
        self.websocket = websocket
        self.deadline = deadline
        self.sequence = 0
        self.messages = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SessionUnavailable("Codex daemon read deadline expired")
        return remaining

    def request(self, method, params):
        if method not in ("initialize", "thread/loaded/list", "thread/read"):
            raise SessionUnavailable("Unsupported Codex observer method")
        if method == "thread/read" and params.get("includeTurns") is not False:
            raise SessionUnavailable("Codex observer requires metadata-only reads")
        self.remaining()
        self.sequence += 1
        self.websocket.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            self.messages += 1
            if self.messages > MAX_MESSAGES:
                raise SessionUnavailable("Codex daemon response inventory exceeded its bound")
            row = json.loads(self.websocket.recv(timeout=self.remaining()))
            if not isinstance(row, dict):
                raise SessionUnavailable("Invalid Codex daemon response")
            if "id" not in row and isinstance(row.get("method"), str):
                continue  # Notifications never establish identity.
            if type(row.get("id")) is not int or row["id"] != self.sequence or "error" in row:
                raise SessionUnavailable("Unexpected Codex daemon response")
            result = row.get("result")
            if not isinstance(result, dict):
                raise SessionUnavailable("Invalid Codex daemon result")
            return result

    def loaded_ids(self):
        ids, cursors = set(), set()
        cursor = None
        for _ in range(MAX_THREADS // PAGE_SIZE + 1):
            result = self.request("thread/loaded/list", {"limit": PAGE_SIZE, "cursor": cursor})
            page = result.get("data")
            if (not isinstance(page, list) or len(page) > PAGE_SIZE
                    or any(not isinstance(tid, str) or not THREAD_ID.fullmatch(tid) for tid in page)
                    or page != sorted(page) or (cursor is not None and page and page[0] <= cursor)
                    or len(set(page)) != len(page) or ids.intersection(page)
                    or "nextCursor" not in result):
                raise SessionUnavailable("Invalid Codex loaded-thread inventory")
            ids.update(page)
            if len(ids) > MAX_THREADS:
                raise SessionUnavailable("Codex loaded-thread inventory exceeded its bound")
            cursor = result["nextCursor"]
            if cursor is None:
                return ids
            if (not isinstance(cursor, str) or not THREAD_ID.fullmatch(cursor)
                    or not page or cursor != page[-1] or cursor in cursors):
                raise SessionUnavailable("Incomplete Codex loaded-thread pagination")
            cursors.add(cursor)
        raise SessionUnavailable("Codex loaded-thread pagination exceeded its bound")

    def read(self, thread_id):
        return self.request("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread")


@contextmanager
def observe_daemon(home, *, timeout=TIMEOUT):
    """One overall deadline includes connect, handshake, sends, reads and close."""
    raw = None
    websocket = None
    timer = None
    try:
        from websockets.sync.client import unix_connect
        deadline = time.monotonic() + timeout
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The watchdog also bounds sends if a broken peer stops consuming data.
        def expire():
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        raw.settimeout(timeout)
        raw.connect(str(socket_path(home)))
        raw.settimeout(None)
        websocket = unix_connect(sock=raw, uri="ws://localhost/rpc", proxy=None,
            open_timeout=max(0.001, deadline - time.monotonic()), close_timeout=0,
            ping_interval=None, compression=None, max_size=1024 * 1024, max_queue=8)
        observer = DaemonObserver(websocket, deadline)
        observer.request("initialize", {"clientInfo": {"name": "herdr_readonly", "version": "0.2.0"},
                                       "capabilities": {"experimentalApi": True}})
        websocket.send(json.dumps({"method": "initialized", "params": {}}))
        yield observer
    except SessionUnavailable:
        raise
    except Exception as exc:
        raise SessionUnavailable("Codex daemon observer unavailable") from exc
    finally:
        # No thread/unsubscribe, shutdown or other lifecycle RPC is sent.
        if websocket is not None:
            websocket.close()
        if raw is not None:
            raw.close()
        if timer is not None:
            timer.cancel()
