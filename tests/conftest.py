from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


def load_transcript(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_agent_homes(tmp_path_factory, monkeypatch):
    """Keep the developer's real Codex/Claude config out of every test."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))


@pytest.fixture
def transcript():
    return load_transcript
