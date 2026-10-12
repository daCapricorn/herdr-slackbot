"""Run the configured, installed Bridge adapter; emit no model-visible output."""
import json
import os
from pathlib import Path
import sys

try:
    root = Path(__file__).resolve().parents[1]
    runtime = json.loads((root / "runtime.json").read_text())
    os.chdir(runtime["projectRoot"])
    os.execv(runtime["python"], [runtime["python"], "-m", "herdr_bridge.agent_hook", "--agent", sys.argv[1]])
except Exception:
    pass
