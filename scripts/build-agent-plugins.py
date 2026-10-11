"""Build two local agent plugins against an installed Bridge runtime, without enabling them."""
import argparse
import json
from pathlib import Path
import shutil
import sys


def build(output, *, project=None, python=None):
    project = Path(project or Path(__file__).resolve().parents[1]).resolve()
    python = str(Path(python or project / ".venv/bin/python").absolute())
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for agent in ("codex", "claude"):
        target = output / agent
        shutil.copytree(project / "plugins" / agent, target, dirs_exist_ok=True)
        (target / "runtime.json").write_text(json.dumps({"projectRoot": str(project), "python": python}) + "\n")
    codex_catalog = {"name": "herdr-bridge-local", "plugins": [{"name": "herdr-bridge-codex", "source": {"source": "local", "path": "./codex"}, "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}
    claude_catalog = {"name": "herdr-bridge-local", "owner": {"name": "Herdr Bridge"}, "plugins": [{"name": "herdr-bridge-claude", "source": "./claude", "description": "Exact Herdr session registration for Claude Code"}]}
    for folder, catalog in ((".agents/plugins", codex_catalog), (".claude-plugin", claude_catalog)):
        root = output / folder
        root.mkdir(parents=True, exist_ok=True)
        (root / "marketplace.json").write_text(json.dumps(catalog, indent=2) + "\n")
    return {"root": str(output), "plugins": ["herdr-bridge-codex", "herdr-bridge-claude"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".agent-plugins")
    parser.add_argument("--python", default=None)
    args = parser.parse_args()
    print(json.dumps(build(args.output, python=args.python)))
