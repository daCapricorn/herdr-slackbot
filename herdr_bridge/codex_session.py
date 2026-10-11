"""Read-only exact/unique Codex session resolution shared by bridge readers."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3

from .events import session_of


class SessionUnavailable(RuntimeError):
    pass


def resolve_session(info, *, home=None, unique_workspace=False, thread_id=None, require_rollout=True):
    root = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    cwd = info.get("cwd")
    native = session_of(info)
    if info.get("agent") != "codex" or not cwd:
        raise SessionUnavailable("Codex workspace unavailable")
    if native and thread_id and native != thread_id:
        raise SessionUnavailable("Bound session differs from Herdr session")
    thread_id = native or thread_id
    if not thread_id and not unique_workspace:
        raise SessionUnavailable("Session id missing; multiple panes may share this directory")
    try:
        databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]))
        if not databases:
            raise SessionUnavailable("Codex database unavailable")
        with closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            condition, value = ("id", thread_id) if thread_id else ("cwd", cwd)
            rows = db.execute(f"SELECT id,rollout_path,cwd FROM threads WHERE {condition}=? AND archived=0 AND agent_path IS NULL", (value,)).fetchall()
        if len(rows) != 1:
            raise SessionUnavailable("Session match ambiguous; bind an exact thread id" if len(rows) > 1 else "Session not found")
        tid, path, recorded_cwd = rows[0]
        if recorded_cwd != cwd or (require_rollout and not path):
            raise SessionUnavailable("Session directory changed or rollout missing")
        return tid, Path(path), recorded_cwd
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise SessionUnavailable("Cannot read Codex session database") from exc


def validate_rollout(session):
    tid, path, cwd = session
    try:
        with path.open("rb") as stream:
            row = json.loads(stream.readline(1024 * 1024))
        if row.get("type") != "session_meta" or row.get("payload", {}).get("id") != tid or row["payload"].get("cwd") != cwd:
            raise SessionUnavailable("Rollout identity differs from database")
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise SessionUnavailable("Cannot validate rollout identity") from exc
    return session
