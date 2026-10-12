"""Agent-specific identity/readers behind one Bridge-facing contract."""
import json
import os
import sys
import time
import tomllib
from pathlib import Path

from .claude_sync import ClaudeTranscriptSync, SESSION_ID, claude_home
from .codex_session import (AUTHORITY_ERROR, TITLE_SOURCE, SessionUnavailable,
                            resolve_title_session, title_session_prefix)
from .codex_sync import CodexRolloutSync

TITLE_ID_ITEMS = ("session-id", "thread-id")
MANAGED_CONFIG_PATH = Path("/etc/codex/managed_config.toml")
SYSTEM_CONFIG_PATH = Path("/etc/codex/config.toml")


def foreground_claude_session(info, process, *, home=None, started=None):
    """Read only a foreground PID's registry; check PID reuse and terminal context."""
    if info.get("agent") != "claude" or process.get("pane_id") != info.get("pane_id"):
        return None
    if started is None:
        import psutil
        started = lambda pid: (time.ctime(psutil.Process(pid).create_time()),
                               time.asctime(time.gmtime(psutil.Process(pid).create_time())))
    candidates = []
    for proc in process.get("foreground_processes", []):
        pid = proc.get("pid")
        if type(pid) is not int or pid <= 0:
            continue
        try:
            record = json.loads((Path(home or claude_home()) / "sessions" / f"{pid}.json").read_text())
            tid = record.get("sessionId")
            if (record.get("pid") == pid and record.get("cwd") == info.get("cwd")
                    and record.get("kind") == "interactive" and record.get("procStart") in ((value,) if isinstance(value := started(pid), str) else value)
                    and isinstance(tid, str) and SESSION_ID.fullmatch(tid)):
                candidates.append(tid)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return candidates[0] if len(candidates) == 1 else None


def codex_argv(proc):
    """argv of a Codex executable (native or npm launcher), else None."""
    argv = proc.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    name = os.path.basename(argv[0])
    if name == "codex" or name.startswith("codex-"):
        return argv
    if name in ("node", "nodejs") and len(argv) > 1 and os.path.basename(argv[1]) in ("codex", "codex.js"):
        return argv[1:]
    return None


def _toml_value(raw):
    # Codex parses override values as TOML and falls back to a literal string.
    try:
        return tomllib.loads("value = " + raw)["value"]
    except tomllib.TOMLDecodeError:
        return raw


def terminal_title_items(config, argv):
    """Bounded title config: user config plus direct CLI title overrides."""
    if "profile" in config:
        raise SessionUnavailable("Codex profile config is unsupported for title identity")
    tui = config.get("tui")
    if tui is not None and not isinstance(tui, dict):
        raise SessionUnavailable("Codex tui config is invalid for title identity")
    items = tui.get("terminal_title") if isinstance(tui, dict) else None
    args = iter(argv[1:])
    for arg in args:
        if arg == "--":
            break
        if arg == "--profile" or arg.startswith("--profile=") or arg.startswith("-p"):
            raise SessionUnavailable("Codex profile selection is unsupported for title identity")
        if arg in ("-c", "--config"):
            override = next(args, "")
        elif arg.startswith("--config="):
            override = arg[len("--config="):]
        elif arg.startswith("-c") and not arg.startswith("--"):
            override = arg[2:].removeprefix("=")
        else:
            continue
        key, _, raw = override.partition("=")
        key = key.strip()
        if key == "tui.terminal_title":
            items = _toml_value(raw.strip())
        elif key == "tui" or key.startswith("tui.terminal_title."):
            raise SessionUnavailable("Codex top-level or nested tui title override is unsupported")
        elif key == "profile" or key == "profiles" or key.startswith("profiles."):
            raise SessionUnavailable("Codex profile override is unsupported for title identity")
    if items is not None and (not isinstance(items, list) or not all(isinstance(item, str) for item in items)):
        raise SessionUnavailable("Codex terminal title config must be a list of strings")
    return items


def exports_session_title(items):
    return isinstance(items, list) and bool(items) and items[0] in TITLE_ID_ITEMS


def forced_codex_preferences():
    """Query Codex's forced preference keys without reading their contents."""
    if sys.platform != "darwin":
        return False
    import ctypes
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringCreateWithCString.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32)
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFPreferencesAppValueIsForced.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    cf.CFPreferencesAppValueIsForced.restype = ctypes.c_ubyte
    cf.CFPreferencesAppSynchronize.argtypes = (ctypes.c_void_p,)
    cf.CFPreferencesAppSynchronize.restype = ctypes.c_ubyte
    cf.CFRelease.argtypes = (ctypes.c_void_p,)
    cf.CFRelease.restype = None
    refs = []
    try:
        for value in (b"com.openai.codex", b"config_toml_base64", b"requirements_toml_base64"):
            ref = cf.CFStringCreateWithCString(None, value, 0x08000100)
            if not ref:
                raise OSError("Cannot query Codex managed preferences")
            refs.append(ref)
        if not cf.CFPreferencesAppSynchronize(refs[0]):
            raise OSError("Cannot synchronize Codex managed preferences")
        return any(cf.CFPreferencesAppValueIsForced(key, refs[0]) for key in refs[1:])
    finally:
        for ref in refs:
            cf.CFRelease(ref)


def read_codex_config(path):
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SessionUnavailable(f"Codex config unreadable or invalid: {path}") from exc


def check_title_config_layers(home, cwd):
    """Reject unmodeled local title layers; no project trust/root inference."""
    try:
        MANAGED_CONFIG_PATH.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SessionUnavailable("Cannot establish absence of Codex managed config") from exc
    else:
        raise SessionUnavailable("Codex managed config is unsupported for title identity")
    try:
        if forced_codex_preferences():
            raise SessionUnavailable("Codex managed preferences are unsupported for title identity")
    except SessionUnavailable:
        raise
    except Exception as exc:
        raise SessionUnavailable("Cannot establish absence of Codex managed preferences") from exc
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise SessionUnavailable("Codex title config needs an absolute pane directory")
    root = Path(home).resolve()
    directory = Path(cwd).resolve()
    paths = [SYSTEM_CONFIG_PATH]
    paths.extend(parent / ".codex/config.toml" for parent in (directory, *directory.parents)
                 if (parent / ".codex").resolve() != root)
    for path in paths:
        config = read_codex_config(path)
        tui = config.get("tui")
        if "profile" in config or (tui is not None and (not isinstance(tui, dict) or "terminal_title" in tui)):
            raise SessionUnavailable(f"Codex title/profile config layer is unsupported: {path}")


class AgentAdapter:
    """Identity enrichment + durable transcript source; no Slack or terminal writes."""
    def __init__(self, source):
        self.source = source

    def enrich(self, info, client):
        return info


class ClaudeAdapter(AgentAdapter):
    def enrich(self, info, client):
        try:
            process = client.process_info(info["pane_id"])
            tid = foreground_claude_session(info, process, home=self.source.home)
            # Revalidate process and terminal after reading the registry.
            if tid:
                fresh = client.get_agent(info["pane_id"])
                check = client.process_info(info["pane_id"])
                if (all(fresh.get(k) == info.get(k) for k in ("pane_id", "terminal_id", "cwd", "agent"))
                        and check == process and foreground_claude_session(fresh, check, home=self.source.home) == tid):
                    return {**info, "agent_session": {"agent": "claude", "kind": "id", "source": "bridge:claude-pid", "value": tid}}
        except Exception:
            pass  # Absence/unsupported registry never licenses a directory guess.
        return info


class CodexAdapter(AgentAdapter):
    """The configured native title is authoritative; uncertainty blocks fallback."""
    def enrich(self, info, client):
        try:
            config = read_codex_config(Path(self.source.home) / "config.toml")
            process = client.process_info(info["pane_id"])
            argvs = [argv for p in process.get("foreground_processes", []) if (argv := codex_argv(p))]
            if process.get("pane_id") != info.get("pane_id") or not argvs:
                raise SessionUnavailable("Codex title config needs a Codex foreground process")
            check_title_config_layers(self.source.home, info.get("cwd"))
            enabled = {exports_session_title(terminal_title_items(config, argv)) for argv in argvs}
            if len(enabled) > 1:
                raise SessionUnavailable("Codex foreground processes disagree on title export")
            if enabled == {False}:
                fresh = client.get_agent(info["pane_id"])
                if (any(fresh.get(k) != info.get(k) for k in ("pane_id", "terminal_id", "cwd", "agent"))
                        or client.process_info(info["pane_id"]) != process):
                    raise SessionUnavailable("Codex pane changed while reading its title config")
                return info
            tid = self._title_session(info, client, process, argvs)
        except SessionUnavailable as exc:
            return self._unavailable(info, str(exc))
        except Exception:
            return self._unavailable(info, "Codex process/config/title identity read failed")
        return {**{k: v for k, v in info.items() if k != AUTHORITY_ERROR},
                "agent_session": {"agent": "codex", "kind": "id", "source": TITLE_SOURCE, "value": tid}}

    def _title_session(self, info, client, process, argvs):
        if process.get("pane_id") != info.get("pane_id") or not argvs:
            raise SessionUnavailable("Codex title identity needs a Codex foreground process")
        prefix = title_session_prefix(info.get("terminal_title_stripped"))
        if not prefix:
            raise SessionUnavailable("Codex title export enabled but the title has no exact session id")
        tid, _, _ = resolve_title_session(prefix, info.get("cwd"), home=self.source.home)
        fresh = client.get_agent(info["pane_id"])
        if (any(fresh.get(k) != info.get(k) for k in ("pane_id", "terminal_id", "cwd", "agent"))
                or title_session_prefix(fresh.get("terminal_title_stripped")) != prefix
                or client.process_info(info["pane_id"]) != process):
            raise SessionUnavailable("Codex pane changed while reading its title identity")
        return tid

    @staticmethod
    def _unavailable(info, reason):
        return {**info, "agent_session": None, AUTHORITY_ERROR: reason}


def adapters(db):
    return {"codex": CodexAdapter(CodexRolloutSync(db)), "claude": ClaudeAdapter(ClaudeTranscriptSync(db))}
