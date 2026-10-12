import importlib.util
import stat
from pathlib import Path

import pytest

from test_codex_title_identity import A, Client, Home, agent, enrich

script = Path(__file__).resolve().parents[1] / "scripts/configure-codex-identity.py"
spec = importlib.util.spec_from_file_location("configure_codex_identity", script)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

EXPORTED = 'terminal_title = ["session-id", "thread", "current-dir"]'
# Codex's unset default is ["activity", "thread-name", "project-name"]; activity is dropped.
DEFAULT = 'terminal_title = ["session-id", "thread-name", "project-name"]'

ORIGINAL = '''# personal settings
model = "gpt-5.5"   # keep this comment
approval_policy = "on-request"

[projects."/Users/me/work"]
trust_level = "trusted"

[tui]
# title items
terminal_title = [
  "thread",   # the thread name
  "spinner",
  "current-dir",
]
notifications = true

[hooks.state]
"a" = { trusted_hash = "abc" }
'''


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_missing_config_gets_default_items_with_leading_session_id(tmp_path):
    result = helper.configure(tmp_path)
    path = tmp_path / "config.toml"
    assert result["changed"] and result["terminal_title"] == ["session-id", "thread-name", "project-name"]
    assert path.read_text() == "[tui]\n" + DEFAULT + "\n"
    assert mode(path) == 0o600
    assert not list(tmp_path.glob("*backup*"))
    assert helper.configure(tmp_path)["changed"] is False


def test_existing_config_bytes_trust_and_hooks_are_preserved_and_backed_up_once(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    path.chmod(0o640)
    result = helper.configure(tmp_path)
    start = ORIGINAL.index("terminal_title")
    end = ORIGINAL.index("]\nnotifications") + 1
    assert path.read_text() == ORIGINAL[:start] + EXPORTED + ORIGINAL[end:]
    assert result["terminal_title"] == ["session-id", "thread", "current-dir"]  # spinner prefixes the title
    assert mode(path) == 0o640
    backup = Path(result["backup"])
    assert backup.read_text() == ORIGINAL and mode(backup) == 0o600
    edited = path.read_bytes()
    again = helper.configure(tmp_path)
    assert again["changed"] is False and path.read_bytes() == edited and backup.read_text() == ORIGINAL
    path.write_text(ORIGINAL)
    helper.configure(tmp_path)
    assert backup.read_text() == ORIGINAL  # the first backup is never overwritten


@pytest.mark.parametrize("before,after", [
    ('[tui]\nnotifications = true\n', '[tui]\n' + DEFAULT + '\nnotifications = true\n'),
    ('[tui]\nterminal_title = ["thread", "current-dir", "session-id"]\n', '[tui]\n' + EXPORTED + '\n'),
    ('[tui]\nterminal_title = ["status", "model"]\n', '[tui]\nterminal_title = ["session-id", "model"]\n'),
    ('[tui]\nterminal_title = ["thread-id", "spinner"]\n', '[tui]\nterminal_title = ["thread-id"]\n'),
    ('[tui]\nterminal_title = ["activity", "thread-name", "project-name"]\n', '[tui]\n' + DEFAULT + '\n'),
    ('[tui]\nterminal_title = ["git-branch", "activity", "model"]\n',
     '[tui]\nterminal_title = ["session-id", "git-branch", "model"]\n'),  # activity anywhere adds a blocked prefix
    ('[tui]\nterminal_title = ["session-id", "thread", "activity"]\n', '[tui]\nterminal_title = ["session-id", "thread"]\n'),
    ('tui.terminal_title = ["thread"] # mine\n', 'tui.terminal_title = ["session-id", "thread"] # mine\n'),
    ('model = "x"\n', 'model = "x"\n\n[tui]\n' + DEFAULT + '\n'),
    ('[tui.notifications]\nx = 1\n', '[tui.notifications]\nx = 1\n\n[tui]\n' + DEFAULT + '\n'),
])
def test_title_items_are_minimally_edited(tmp_path, before, after):
    path = tmp_path / "config.toml"
    path.write_text(before)
    assert helper.configure(tmp_path)["changed"]
    assert path.read_text() == after


@pytest.mark.parametrize("items", ['"session-id", "model"', '"thread-id", "model"', '"thread-id"', '"session-id"'])
def test_already_exported_config_is_not_rewritten(tmp_path, items):
    path = tmp_path / "config.toml"
    path.write_text(f'[tui]\nterminal_title = [{items}]\n')
    before = path.stat().st_mtime_ns
    assert helper.configure(tmp_path)["changed"] is False
    assert path.stat().st_mtime_ns == before and not list(tmp_path.glob("*backup*"))


@pytest.mark.parametrize("text", [
    "[tui\nterminal_title = [\n",
    '[tui]\nterminal_title = ["thread"]\nterminal_title = ["model"]\n',
    'tui = { terminal_title = ["thread"] }\n',
    '[tui]\nterminal_title = "thread"\n',
    '[tui]\nterminal_title = ["thread", 3]\n',
    'tui.notifications = true\n',
])
def test_malformed_duplicate_or_unsupported_layout_is_refused_without_writing(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(helper.ConfigError):
        helper.configure(tmp_path)
    assert path.read_text() == text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_symlinked_config_is_refused(tmp_path):
    target = tmp_path / "dotfiles.toml"
    target.write_text("[tui]\n")
    (tmp_path / "config.toml").symlink_to(target)
    with pytest.raises(helper.ConfigError):
        helper.configure(tmp_path)
    assert target.read_text() == "[tui]\n"


def test_concurrent_change_after_read_is_refused(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[tui]\n")
    original = path.read_bytes()
    path.write_text('[tui]\nmodel = "changed elsewhere"\n')
    with pytest.raises(helper.ConfigError):
        helper.replace_if_unchanged(path, original, b"[tui]\n" + EXPORTED.encode() + b"\n")
    assert path.read_text() == '[tui]\nmodel = "changed elsewhere"\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_configured_home_enables_exact_title_identity(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, config=None)
    home.thread(A)
    client = Client(agent())
    assert enrich(client)["agent_session"] is None  # unconfigured: existing native contract
    helper.configure(home.path)
    assert enrich(client)["agent_session"]["value"] == A
