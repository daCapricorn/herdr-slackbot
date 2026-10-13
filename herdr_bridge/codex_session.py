"""Read-only exact/unique Codex session resolution shared by bridge readers."""
from contextlib import closing
from pathlib import Path
import json
import os
import re
import sqlite3
from datetime import datetime

from .events import session_of

# Enriched-info field: Codex title export is enabled but cannot name the
# current thread. It overrides native, saved and directory identities.
AUTHORITY_ERROR = "authoritative_session_error"
TITLE_SOURCE = "bridge:codex-title"
LIVE_PROOF = "codex_live_identity"
# Codex's session-id title item: a full UUID or its first 29 characters + "...".
TITLE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-(?:[0-9a-f]{12}|[0-9a-f]{5}\.\.\.)")
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Codex appends " " + one of these frames to the item while a thread title is generated.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TITLE_FIELD = re.compile(f"({TITLE_ID.pattern})(?: [{SPINNER_FRAMES}])?")


class SessionUnavailable(RuntimeError):
    pass


def _state_db(root):
    databases = _state_databases(root)
    if not databases:
        raise SessionUnavailable("Codex database unavailable")
    return closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2))


def _state_databases(root):
    # iterdir reports directory permission/I/O failures. glob may suppress them,
    # which must never be confused with a fresh home having no database yet.
    return sorted((path for path in root.iterdir() if path.name.startswith("state_") and path.name.endswith(".sqlite")),
                  key=lambda path: int(path.stem.split("_")[-1]))


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


def title_rows(prefix, *, home):
    """Collision inventory includes archived, child and other-workspace threads."""
    if not isinstance(prefix, str) or not TITLE_ID.fullmatch(prefix):
        raise SessionUnavailable("Codex title has no exact session id")
    root = Path(home)
    try:
        if not _state_databases(root):
            return []  # A new native home may not yet have a state database.
        with _state_db(root) as db:
            condition, value = (("length(id)=36 AND substr(id,1,29)=?", prefix[:29])
                                if prefix.endswith("...") else ("id=?", prefix))
            rows = db.execute("SELECT id,rollout_path,cwd,archived,agent_path FROM threads WHERE "
                              + condition + " LIMIT 4097", (value,)).fetchall()
        if len(rows) > 4096:
            raise SessionUnavailable("Codex title collision inventory exceeded its bound")
        if any(not isinstance(row[0], str) or not THREAD_ID.fullmatch(row[0]) for row in rows):
            raise SessionUnavailable("Codex database thread id is invalid")
        return rows
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise SessionUnavailable("Cannot read Codex session database") from exc


def native_rollout_path(value, tid, home):
    """Fresh proof only permits the native, local, dated sessions path."""
    try:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("path")
        path = Path(value)
        relative = path.relative_to(Path(home))
        if len(relative.parts) != 5 or relative.parts[0] != "sessions" or ".." in relative.parts:
            raise ValueError("layout")
        match = re.fullmatch(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-" + re.escape(tid) + r"\.jsonl", relative.name)
        if not match:
            raise ValueError("filename")
        stamp = datetime.strptime(match[1], "%Y-%m-%dT%H-%M-%S")
        if relative.parts[1:4] != (f"{stamp.year:04}", f"{stamp.month:02}", f"{stamp.day:02}"):
            raise ValueError("date")
        # Reject escaping symlinks, including an existing rollout symlink.
        if path.resolve() != Path(home).resolve() / relative:
            raise ValueError("symlink")
        return path
    except (OSError, ValueError, TypeError) as exc:
        raise SessionUnavailable("Codex live rollout path is not a native local path") from exc


def resolve_live_title(prefix, cwd, home, observer):
    rows = title_rows(prefix, home=home)
    loaded = observer.loaded_ids()
    matches = {tid for tid in loaded if (tid.startswith(prefix[:29]) if prefix.endswith("...") else tid == prefix)}
    candidates = {row[0] for row in rows} | matches
    if len(candidates) != 1:
        raise SessionUnavailable("Codex title session prefix is ambiguous" if candidates else "Codex title session not found")
    tid = next(iter(candidates))
    if tid not in loaded:
        # The daemon inventory was complete; existing persisted-only behavior is
        # retained for unloaded threads, including strict rollout validation.
        return resolve_title_session(prefix, cwd, home=home), None
    thread = observer.read(tid)
    if (not isinstance(thread, dict) or thread.get("id") != tid or thread.get("cwd") != cwd
            or thread.get("source") not in ("cli", "vscode")
            or "parentThreadId" not in thread or thread["parentThreadId"] is not None
            or thread.get("ephemeral") is not False or thread.get("canAcceptDirectInput") is not True
            or not isinstance(thread.get("status"), dict)
            or thread["status"].get("type") not in ("idle", "active", "systemError")):
        raise SessionUnavailable("Codex live metadata is not a loaded direct-input root thread")
    path = native_rollout_path(thread.get("path"), tid, home)
    if rows:
        if (len(rows) != 1 or rows[0][2] != cwd or rows[0][3] != 0 or rows[0][4] is not None
                or rows[0][1] != str(path)):
            raise SessionUnavailable("Codex live metadata disagrees with persisted root identity")
        return validate_rollout((tid, path, cwd)), None
    # Only absence of an exact row licenses the fresh proof. Corrupt existing
    # files must still fail; ENOENT alone is the narrowly allowed fresh case.
    try:
        path.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SessionUnavailable("Cannot inspect Codex live rollout") from exc
    else:
        validate_rollout((tid, path, cwd))
    return (tid, path, cwd), {"thread_id": tid, "cwd": cwd, "home": str(Path(home)), "path": str(path)}


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
