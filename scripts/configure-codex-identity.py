"""Make Codex title its current session id first so Bridge can map shared-daemon panes.

Edits only tui.terminal_title in config.toml. Hooks, trust, auth, model and
runtime permissions are untouched. Running Codex clients keep their old title
settings until restarted.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib

ID_ITEMS = ("session-id", "thread-id")
# Codex's default title is ["activity", "thread-name", "project-name"].
DEFAULT_ITEMS = ["thread-name", "project-name"]
# Activity items render before the first title item; `activity`/`spinner`
# anywhere also add an "Action Required" prefix while Codex is blocked.
PREFIX_ITEMS = ("activity", "spinner", "status")
BACKUP = "config.toml.herdr-bridge-backup"
TUI_TABLE = re.compile(r"^[ \t]*\[[ \t]*tui[ \t]*\][ \t]*(?:#.*)?$", re.M)
TABLE = re.compile(r"^[ \t]*\[", re.M)
TABLE_KEY = re.compile(r"^[ \t]*terminal_title[ \t]*=[ \t]*", re.M)
DOTTED_KEY = re.compile(r"^[ \t]*tui[ \t]*\.[ \t]*terminal_title[ \t]*=[ \t]*", re.M)
INLINE_TUI = re.compile(r"^[ \t]*tui[ \t]*=", re.M)


class ConfigError(ValueError):
    pass


def desired(items):
    if items is None:
        items = DEFAULT_ITEMS
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ConfigError("tui.terminal_title must be a list of strings; edit it manually")
    kept = [i for i in items if i not in PREFIX_ITEMS]
    if kept and kept[0] in ID_ITEMS:
        return kept
    return ["session-id"] + [i for i in kept if i not in ID_ITEMS]


def toml_array(items):
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def _array_end(text, start):
    """Index after the array opening at text[start]; validation re-parses the result."""
    if text[start:start + 1] != "[":
        raise ConfigError("tui.terminal_title is not an inline array; edit it manually")
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif c in "\"'":
            i += 1
            while i < len(text) and text[i] != c:
                i += 2 if c == '"' and text[i] == "\\" else 1
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ConfigError("Unterminated tui.terminal_title array")


def edit(text, items):
    """Replace or add only the terminal_title value; every other byte is kept."""
    value = toml_array(items)
    tables = list(TUI_TABLE.finditer(text))
    first_table = TABLE.search(text)
    root = text[:first_table.start()] if first_table else text
    if tables:
        body = tables[0].end()
        following = TABLE.search(text, body + 1)
        keys = list(TABLE_KEY.finditer(text, body, following.start() if following else len(text)))
        if not keys:
            return text[:body] + "\nterminal_title = " + value + text[body:]
        start = keys[0].end()
    elif keys := list(DOTTED_KEY.finditer(root)):
        start = keys[0].end()
    elif INLINE_TUI.search(root):
        raise ConfigError("An inline tui table is unsupported; edit tui.terminal_title manually")
    else:
        separator = "" if not text else "\n" if text.endswith("\n") else "\n\n"
        return text + separator + "[tui]\nterminal_title = " + value + "\n"
    return text[:start] + value + text[_array_end(text, start):]


def validate(text, parsed, items):
    try:
        result = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("The edit would not be valid TOML; config.toml left unchanged") from exc
    expected = {**parsed, "tui": {**parsed.get("tui", {}), "terminal_title": items}}
    if result != expected:
        raise ConfigError("The edit would change other settings; config.toml left unchanged")


def replace_if_unchanged(path, original, data):
    """Atomically replace config.toml unless another writer changed it after it was read."""
    temporary = path.with_name(f".config.toml.herdr-bridge-{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current, mode = path.read_bytes(), stat.S_IMODE(path.lstat().st_mode)
        except FileNotFoundError:
            current, mode = None, 0o600
        if current != original:
            raise ConfigError("config.toml changed while editing; nothing was written")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure(home):
    path = Path(home) / "config.toml"
    if path.is_symlink():
        raise ConfigError(f"{path} is a symlink; edit its target manually")
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        original = None
    try:
        text = (original or b"").decode()
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("config.toml is not valid TOML; nothing was changed") from exc
    tui = parsed.get("tui", {})
    if not isinstance(tui, dict):
        raise ConfigError("tui is not a table; edit it manually")
    items = desired(tui.get("terminal_title"))
    result = {"path": str(path), "terminal_title": items, "changed": False, "backup": None,
              "next": "Restart existing Codex clients; running clients keep their old title settings."}
    if items == tui.get("terminal_title"):
        return result
    updated = edit(text, items)
    validate(updated, parsed, items)
    if original is not None:
        backup = path.with_name(BACKUP)
        result["backup"] = str(backup)
        try:
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(original)
        except FileExistsError:
            pass  # Keep the first, pre-Bridge backup.
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_if_unchanged(path, original, updated.encode())
    result["changed"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args.home), indent=2))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
