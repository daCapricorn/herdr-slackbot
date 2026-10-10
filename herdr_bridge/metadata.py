"""Read display metadata from Herdr and current agent sessions, never defaults."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .events import session_of


def _last_model(path, kind):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 512 * 1024))
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if kind == "codex" and row.get("type") == "turn_context":
                return row.get("payload", {}).get("model")
            if kind == "claude" and row.get("type") == "assistant":
                model = row.get("message", {}).get("model")
                if model and model != "<synthetic>":
                    return model
    except (OSError, TypeError):
        pass
    return None


def session_model(info, *, unique_workspace=False, codex_home=None, claude_home=None):
    if info.get("model"):
        return info["model"]
    kind, session = info.get("agent"), session_of(info)
    if kind == "codex":
        home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        databases = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]))
        if not databases:
            return None
        try:
            with closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
                if session:
                    rows = db.execute("SELECT model, rollout_path FROM threads WHERE id=?", (session,)).fetchall()
                elif unique_workspace and info.get("cwd"):
                    # Only one non-subagent session AND one live pane in this cwd.
                    # Ambiguous matches stay unknown instead of using newest/default.
                    rows = db.execute("SELECT model, rollout_path FROM threads WHERE cwd=? AND archived=0 AND agent_path IS NULL",
                                      (info["cwd"],)).fetchall()
                else:
                    return None
                if len(rows) == 1:
                    return _last_model(rows[0][1], kind) or rows[0][0]
        except (sqlite3.Error, OSError, ValueError):
            pass
    elif kind == "claude" and session and info.get("cwd"):
        home = Path(claude_home or Path.home() / ".claude")
        # Match the exact session filename rather than assuming a cwd slug scheme.
        paths = list((home / "projects").glob(f"*/{session}.jsonl"))
        if len(paths) == 1:
            return _last_model(paths[0], kind)
    return None
