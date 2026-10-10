#!/bin/sh
# macOS install/build: isolated Python environment, then config skeleton or wizard.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_python="$task_root/.venv/bin/python"

if [ ! -x "$task_python" ]; then
    task_base_python=""
    for candidate in "${HERDR_SLACKBOT_PYTHON:-}" python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            task_base_python="$candidate"
            break
        fi
    done
    if [ -z "$task_base_python" ]; then
        echo 'herdr-slackbot: Python 3.11+ is required. Install it with brew install python, or set HERDR_SLACKBOT_PYTHON.' >&2
        exit 1
    fi
    "$task_base_python" -m venv "$task_root/.venv"
fi
"$task_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo 'herdr-slackbot: the existing .venv requires Python 3.11+.' >&2
    exit 1
}
if ! "$task_python" -m pip --version >/dev/null 2>&1; then
    "$task_python" -m ensurepip
fi
"$task_python" -m pip install --disable-pip-version-check --editable "$task_root"
cd "$task_root"
case "${1:-}" in
    --skip-config) exit 0 ;;
    --wizard) shift; exec "$task_python" -m herdr_slackbot wizard "$@" ;;
    *) exec "$task_python" -m herdr_slackbot setup "$@" ;;
esac
