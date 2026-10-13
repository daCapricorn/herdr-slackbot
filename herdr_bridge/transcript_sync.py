"""Shared durable transcript contract: atomic cursor, dedupe, outbox and completion."""
import json
import sqlite3
from pathlib import Path
from .events import session_of
from .codex_session import AUTHORITY_ERROR, TITLE_SOURCE, SessionUnavailable
from .session_observation import load_observation

class TranscriptSync:
    agent_kind = ""
    namespace = ""

    def __init__(self, db, *, home=None):
        self.db = db
        self.home = Path(home or Path.home() / ("." + self.agent_kind))
        if self.namespace not in ("codex", "claude"):
            raise ValueError("Unsupported transcript namespace")
        self.db.execute(f"""CREATE TABLE IF NOT EXISTS {self.namespace}_sync_cursors
            (source_key TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, device INTEGER NOT NULL,
             inode INTEGER NOT NULL, offset INTEGER NOT NULL)""")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {self.namespace}_sync_seen (event_id TEXT PRIMARY KEY)")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {self.namespace}_session_bindings (terminal_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, cwd TEXT NOT NULL)")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {self.namespace}_sync_status (terminal_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {self.namespace}_sync_final (source_key TEXT PRIMARY KEY, event_id TEXT NOT NULL)")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {self.namespace}_completion_seen (event_id TEXT PRIMARY KEY)")

    def diagnostic(self, info):
        key = info.get("terminal_id") or info.get("pane_id", "")
        row = self.db.execute(f"SELECT record FROM {self.namespace}_sync_status WHERE terminal_id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {"state": "pending", "reason": "Not scanned yet"}

    def receipt_baseline(self, info):
        """Snapshot the exact session file before a terminal prompt write."""
        thread_id, path, _ = self._resolve(info)
        stat = path.stat()
        return {"source_key": f"{info.get('terminal_id') or info.get('pane_id', '')}:{thread_id}",
                "path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
                "offset": stat.st_size}

    def receipt_recorded(self, info, baseline, expected):
        """Find one exact native user record strictly after the baseline."""
        thread_id, path, _ = self._resolve(info)
        key = f"{info.get('terminal_id') or info.get('pane_id', '')}:{thread_id}"
        stat = path.stat()
        same_file = (baseline.get("missing") is True
                     or stat.st_dev == baseline.get("device") and stat.st_ino == baseline.get("inode"))
        if (key != baseline.get("source_key") or str(path) != baseline.get("path") or not same_file
                or stat.st_size < baseline.get("offset", -1)):
            return False
        with path.open("rb") as stream:
            stream.seek(baseline["offset"])
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and expected in self.receipt_user_texts(row, stream.tell() - len(line)):
                    return True
        return False

    def receipt_user_texts(self, row, offset):
        return ()

    def initial_observation(self, info, session):
        return load_observation(self.home, info, session)

    def _status(self, info, state, **fields):
        key = info.get("terminal_id") or info.get("pane_id", "")
        self.db.execute(f"INSERT OR REPLACE INTO {self.namespace}_sync_status VALUES (?, ?)",
                        (key, json.dumps({"state": state, **fields})))

    def claim_completion(self, info):
        if info.get(AUTHORITY_ERROR):
            return False
        if info.get("agent") != self.agent_kind:
            return True
        if self.diagnostic(info).get("state") != "syncing":
            return not self._has_tracked_source(info)
        try:
            tid, _, _ = self._resolve(info)
        except (SessionUnavailable, OSError):
            return False
        key = f"{info.get('terminal_id') or info.get('pane_id', '')}:{tid}"
        row = self.db.execute(f"SELECT event_id FROM {self.namespace}_sync_final WHERE source_key=?", (key,)).fetchone()
        if not row:
            return False  # Resume/startup without a new answer is not a new completion.
        return bool(self.db.execute(f"INSERT OR IGNORE INTO {self.namespace}_completion_seen VALUES (?)", (row[0],)).rowcount)

    def _has_tracked_source(self, info):
        terminal = info.get("terminal_id") or info.get("pane_id", "")
        if self.db.execute(f"SELECT 1 FROM {self.namespace}_session_bindings WHERE terminal_id=?",
                           (terminal,)).fetchone():
            return True
        prefix = terminal + ":"
        rows = self.db.execute(f"SELECT source_key FROM {self.namespace}_sync_cursors WHERE substr(source_key, 1, ?) = ?",
                               (len(prefix), prefix))
        return any(row[0].rsplit(":", 1)[0] == terminal for row in rows)

    def sync(self, info, enqueue, *, unique_workspace=False):
        self.db.execute(f"SAVEPOINT {self.namespace}_sync_read")
        try:
            result = self._sync(info, enqueue, unique_workspace=unique_workspace)
            self.db.execute(f"RELEASE {self.namespace}_sync_read")
            return result
        except Exception as exc:
            self.db.execute(f"ROLLBACK TO {self.namespace}_sync_read")
            self.db.execute(f"RELEASE {self.namespace}_sync_read")
            if isinstance(exc, (OSError, sqlite3.Error, SessionUnavailable)):
                self._status(info, "unavailable", reason=f"Transcript read failed: {type(exc).__name__}")
                return 0
            raise

    def _sync(self, info, enqueue, *, unique_workspace=False):
        try:
            session = self._resolve(info, unique_workspace)
            thread_id, path, _ = session
        except SessionUnavailable as exc:
            self._status(info, "unavailable", reason=str(exc))
            return 0
        try:
            stat = path.stat()
            terminal = info.get("terminal_id") or info.get("pane_id", "")
            source_key = f"{terminal}:{thread_id}"
            if session_of(info) and info.get("terminal_id") and info["agent_session"].get("source") != TITLE_SOURCE:
                self.db.execute(f"INSERT OR REPLACE INTO {self.namespace}_session_bindings VALUES (?, ?, ?)",
                                (terminal, thread_id, info["cwd"]))
            row = self.db.execute(f"SELECT rollout_path,device,inode,offset FROM {self.namespace}_sync_cursors WHERE source_key=?",
                                  (source_key,)).fetchone()
            same_file = row and row[0] == str(path) and row[1] == stat.st_dev and row[2] == stat.st_ino
            if same_file and stat.st_size >= row[3]:
                offset = row[3]
            elif not row and (observation := self.initial_observation(info, session)):
                offset = observation["offset"]
            elif info.get("agent_status") in ("working", "blocked"):
                offset = self.latest_turn_offset(path)
            else:
                offset = stat.st_size
            if row and (not same_file or stat.st_size < row[3]):
                self.reset_source(source_key)
            cursor = offset
            with path.open("rb") as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    next_cursor = stream.tell()
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise ValueError("invalid rollout row")
                    except (ValueError, UnicodeDecodeError):
                        cursor = next_cursor
                        continue
                    for event_id, event in self.row_events(source_key, raw, cursor):
                        self.emit(event_id, info, event, source_key, enqueue)
                    cursor = next_cursor
                    self.db.execute(f"""INSERT INTO {self.namespace}_sync_cursors
                        (source_key,rollout_path,device,inode,offset) VALUES (?,?,?,?,?)
                        ON CONFLICT(source_key) DO UPDATE SET rollout_path=excluded.rollout_path,
                        device=excluded.device,inode=excluded.inode,offset=excluded.offset""",
                        (source_key, str(path), stat.st_dev, stat.st_ino, cursor))
            # Never flush a final message while its transcript has an incomplete tail.
            for event_id, event in self.finish_events(source_key, info, cursor == stat.st_size):
                self.emit(event_id, info, event, source_key, enqueue)
            # Empty/settled rollouts also need a baseline cursor before the next turn.
            self.db.execute(f"""INSERT OR REPLACE INTO {self.namespace}_sync_cursors
                (source_key,rollout_path,device,inode,offset) VALUES (?,?,?,?,?)""",
                (source_key, str(path), stat.st_dev, stat.st_ino, cursor))
            self._status(info, "syncing", thread_id=thread_id, offset=cursor)
            return cursor - offset
        except (OSError, sqlite3.Error):
            raise  # The enclosing savepoint also rolls back seen IDs and outbox writes.

    def emit(self, event_id, info, event, source_key, enqueue):
        inserted = self.db.execute(f"INSERT OR IGNORE INTO {self.namespace}_sync_seen VALUES (?)",
                                   (event_id,)).rowcount
        if inserted:
            enqueue(event_id, info, event)
            if event["role"] == "assistant":
                self.db.execute(f"INSERT OR REPLACE INTO {self.namespace}_sync_final VALUES (?, ?)",
                                (source_key, event_id))

    def finish_events(self, source_key, info, complete):
        return []

    def reset_source(self, source_key):
        pass
