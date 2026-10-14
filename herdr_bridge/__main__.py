"""Private NDJSON stdio worker supervised by the OpenClaw plugin."""
import argparse
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .events import SubscriptionManager
from .herdr_client import HerdrClient
from .core import Bridge
from .errors import BridgeError, error_payload


def response_for_request(line, methods):
    """Execute once; unclassified failures after invocation remain unknown."""
    request = {}
    invoked = False
    try:
        request = json.loads(line)
        method = request["method"]
        if method not in methods:
            raise BridgeError("Unknown bridge method", code="unknown_method", outcome="not_sent")
        # Resolve arguments before invoking any method. Serialization/shape
        # failures here cannot have reached terminal-effect code.
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise BridgeError("Bridge params must be an object", code="invalid_request", outcome="not_sent")
        invoked = True
        result = methods[method](**params)
        return {"id": request.get("id"), "result": result}
    except Exception as exc:
        if not invoked and not isinstance(exc, BridgeError):
            exc = BridgeError(str(exc), code="invalid_request", outcome="not_sent")
        return {"id": request.get("id") if isinstance(request, dict) else None,
                "error": error_payload(exc, outcome=None if invoked else "not_sent")}


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
                bridge.reconcile_answers()
                bridge.reconcile_inputs()
                for info in client.list_agents():
                    bridge.discover(info)
            except Exception:
                logging.exception("Herdr reconciliation failed; retrying")
            stopped.wait(5)

    manager.start()
    thread = threading.Thread(target=scan, daemon=True)
    thread.start()
    methods = {name: getattr(bridge, name) for name in (
        "status", "read", "pending", "sync_status", "bind_session", "events", "ack", "answer", "send", "ingest_input",
        "check", "retry", "prepare_answer", "dispatch_reserved", "bind_card", "result_cards", "event_for_delivery",
        "pane_channels", "channel_provisioned", "channel_ready", "channel_archived", "pane_for_channel",
        "route_event", "authorize_request")}
    output_lock = threading.Lock()

    def handle(line):
        response = response_for_request(line, methods)
        with output_lock:
            print(json.dumps(response, ensure_ascii=False), flush=True)

    try:
        # A slow approval check must not prevent a duplicate click/status query.
        with ThreadPoolExecutor(max_workers=4) as executor:
            for line in sys.stdin:
                executor.submit(handle, line)
    finally:
        stopped.set()
        manager.stop()
        thread.join(timeout=8)
        if not thread.is_alive():
            bridge.close()
        state_lock.close()


if __name__ == "__main__":
    main()
