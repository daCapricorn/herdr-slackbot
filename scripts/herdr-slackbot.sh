#!/bin/sh
# macOS plugin entrypoint; absolute paths keep spaces in the checkout safe.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_python="$task_root/.venv/bin/python"
if [ ! -x "$task_python" ] || ! "$task_python" -c 'import slack_bolt, slack_sdk, psutil' 2>/dev/null; then
    sh "$task_root/scripts/setup.sh"
fi
cd "$task_root"
exec "$task_python" -m herdr_slackbot "$@"
