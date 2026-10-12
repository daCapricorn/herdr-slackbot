"""Read-only exact/unique Codex session resolution shared by bridge readers."""
from contextlib import closing
from pathlib import Path
import json
import os
import re
import sqlite3

from .events import session_of

# Enriched-info field: Codex title export is enabled but cannot name the
# current thread. It overrides native, saved and directory identities.
AUTHORITY_ERROR = "authoritative_session_error"
TITLE_SOURCE = "bridge:codex-title"
# Codex's session-id title item: a full UUID or its first 29 characters + "...".
TITLE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-(?:[0-9a-f]{12}|[0-9a-f]{5}\.\.\.)")
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Codex appends " " + one of these frames to the item while a thread title is generated.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TITLE_FIELD = re.compile(f"({TITLE_ID.pattern})(?: [{SPINNER_FRAMES}])?")


class SessionUnavailable(RuntimeError):
    pass


def _state_db(root):
    databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]))
    if not databases:
        raise SessionUnavailable("Codex database unavailable")
    return closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2))


def resolve_session(info, *, home=None, unique_workspace=False, thread_id=None, require_rollout=True):
    if info.get(AUTHORITY_ERROR):
        raise SessionUnavailable(info[AUTHORITY_ERROR])
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
        with _state_db(root) as db:
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


def title_session_prefix(title):
    """The leading session-id title item only; prose and later items are never searched."""
    first = title.split(" | ", 1)[0] if isinstance(title, str) else ""
    match = TITLE_FIELD.fullmatch(first)
    return match.group(1) if match else None


def resolve_title_session(prefix, cwd, *, home=None):
    """Resolve a title identifier to the one root thread it names in this directory."""
    if not isinstance(prefix, str) or not TITLE_ID.fullmatch(prefix):
        raise SessionUnavailable("Codex title has no exact session id")
    root = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    columns = "SELECT id,rollout_path,cwd,archived,agent_path FROM threads"
    try:
        with _state_db(root) as db:
            if prefix.endswith("..."):
                # Every thread sharing the prefix counts, including other directories and children.
                rows = db.execute(columns + " WHERE length(id)=36 AND substr(id,1,29)=?", (prefix[:29],)).fetchall()
            else:
                rows = db.execute(columns + " WHERE id=?", (prefix,)).fetchall()
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise SessionUnavailable("Cannot read Codex session database") from exc
    if not rows:
        raise SessionUnavailable("Codex title session not found")
    if len(rows) > 1:
        raise SessionUnavailable("Codex title session prefix is ambiguous")
    tid, path, recorded_cwd, archived, agent_path = rows[0]
    if not isinstance(tid, str) or not THREAD_ID.fullmatch(tid):
        raise SessionUnavailable("Codex title session id is not a valid thread id")
    if archived != 0 or agent_path is not None:
        raise SessionUnavailable("Codex title session is not an active root thread")
    if recorded_cwd != cwd or not path:
        raise SessionUnavailable("Codex title session directory differs from pane or rollout missing")
    return validate_rollout((tid, Path(path), recorded_cwd))


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
