"""Add Bridge hooks alongside existing Codex hooks; trust stays user-managed."""
import argparse
import json
from pathlib import Path
import shlex
import sys


def install(home, python=sys.executable, project=None):
    project = Path(project or Path(__file__).resolve().parents[1])
    path = Path(home) / "hooks.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    hooks = config.setdefault("hooks", {})
    command = f"cd {shlex.quote(str(project))} && {shlex.quote(str(python))} -m herdr_bridge.session_hook"
    handler = {"type": "command", "command": command, "timeout": 5}
    changed = False
    for event in ("SessionStart", "UserPromptSubmit"):
        groups = hooks.setdefault(event, [])
        # Updating our own hook must not reorder or modify third-party hooks.
        existing = next((h for g in groups for h in g.get("hooks", [])
                         if "-m herdr_bridge.session_hook" in h.get("command", "")), None)
        if existing is None:
            groups.append({"hooks": [handler.copy()]})
            changed = True
        elif existing != handler:
            existing.clear()
            existing.update(handler)
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name("hooks.json.herdr-bridge-backup")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
        temporary = path.with_name("hooks.json.herdr-bridge-tmp")
        temporary.write_text(json.dumps(config, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    return {"path": str(path), "changed": changed, "command": command,
            "next": "Review and trust the two Bridge hooks with /hooks in Codex. No trust settings were changed."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    print(json.dumps(install(args.home), indent=2))
