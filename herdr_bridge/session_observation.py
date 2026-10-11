"""Terminal-scoped hook evidence for the exact start of a conversation stream."""
import json
import os
from pathlib import Path
import re


def observation_path(home, terminal):
    if not isinstance(terminal, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", terminal):
        raise ValueError("Invalid terminal identity")
    return Path(home) / "herdr-bridge/sessions" / (terminal + ".json")


def load_observation(home, info, session):
    tid, path, cwd = session
    try:
        record = json.loads(observation_path(home, info.get("terminal_id")).read_text())
        if not isinstance(record, dict):
            return None
        stat = path.stat()
        if (record.get("terminal_id") == info["terminal_id"] and record.get("thread_id") == tid
                and record.get("cwd") == cwd and record.get("path") == str(path)
                and record.get("device") == stat.st_dev and record.get("inode") == stat.st_ino
                and type(record.get("offset")) is int and 0 <= record["offset"] <= stat.st_size):
            return record
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def save_observation(home, info, session, seq, offset):
    # Hooks can overlap; compare invocation sequence under a nonblocking lock.
    import fcntl
    target = observation_path(home, info["terminal_id"])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.with_suffix(".lock").open("a+b") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            current = json.loads(target.read_text())
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        if type(current.get("seq")) is int and current["seq"] >= seq:
            return False
        tid, path, cwd = session
        stat = path.stat()
        # Repeated prompts/compaction in the same session retain its first cutoff.
        matching = load_observation(home, info, session)
        record = {"terminal_id": info["terminal_id"], "thread_id": tid, "cwd": cwd,
                  "path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
                  "offset": matching["offset"] if matching else offset, "seq": seq}
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record) + "\n")
        temporary.chmod(0o600)
        temporary.replace(target)
        return True
