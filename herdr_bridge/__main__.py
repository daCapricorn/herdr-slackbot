"""Private NDJSON stdio worker supervised by the OpenClaw plugin."""
import argparse
import json
import logging
import os
import sys
import threading

from .events import SubscriptionManager
from .herdr_client import HerdrClient
from .core import Bridge


def lock_state(path):
    """One authority per state database; managed child processes may overlap on reload."""
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(path + ".lock", "a+b")
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--herdr-bin", default="herdr")
    args = parser.parse_args()
    if os.name != "nt":
        os.umask(0o077)
    state_lock = lock_state(args.state)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    client = HerdrClient.from_env(args.socket, args.herdr_bin)
    bridge = Bridge(client, args.state)
    manager = SubscriptionManager(client, bridge.transition, resync_interval=10)
    stopped = threading.Event()

    def scan():
        while not stopped.is_set():
            try:
                for info in client.list_agents():
                    bridge.discover(info)
            except Exception:
                logging.exception("Herdr reconciliation failed; retrying")
            stopped.wait(5)

    manager.start()
    thread = threading.Thread(target=scan, daemon=True)
    thread.start()
    methods = {name: getattr(bridge, name) for name in ("status", "read", "pending", "events", "ack", "answer", "send")}
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                method = request["method"]
                if method not in methods:
                    raise ValueError("Unknown bridge method")
                result = methods[method](**request.get("params", {}))
                response = {"id": request.get("id"), "result": result}
            except Exception as exc:
                response = {"id": request.get("id") if isinstance(request, dict) else None,
                            "error": {"code": getattr(exc, "code", "bridge_error"), "message": str(exc)}}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        stopped.set()
        manager.stop()
        thread.join(timeout=8)
        if not thread.is_alive():
            bridge.close()
        state_lock.close()


if __name__ == "__main__":
    main()
