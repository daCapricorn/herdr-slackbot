"""Send one harmless Markdown rendering sample to the owner's existing thread."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from herdr_slackbot.config import load_config
from herdr_slackbot import blocks as B


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, required=True)
    args = parser.parse_args()
    cfg = load_config(config_dir=args.config_dir)
    state = json.loads((cfg.state_dir / "state.json").read_text())
    threads = [t for t in state.get("threads", {}).values() if t.get("channel") and t.get("thread_ts")]
    if not threads:
        raise SystemExit("No existing owner thread to test in")
    thread = max(threads, key=lambda t: float(t.get("created_at") or 0))
    sample = (
        "› 请查看项目目录。\n"
        "• 下面是命令示例，仅用于展示，未执行。\n"
        "• 连续说明放在一起，不加分隔线。\n"
        "• Ran ls -ld '/Volumes/My Shared Files/AI/herdr-slackbot'\n"
        "  └ drwxr-xr-x  user staff  herdr-slackbot（示例输出）\n"
        "• 命令与输出之后，继续正常正文。\n"
    )
    blocks, _ = B.result_blocks("*聊天排版测试 · 原生 Markdown*", [], sample, None, markdown=False)
    client = WebClient(token=cfg.slack_bot_token, retry_handlers=[], timeout=20)
    try:
        response = client.chat_postMessage(
            channel=thread["channel"], thread_ts=thread["thread_ts"],
            text="聊天排版测试：原生 Markdown 和 Bash 代码块（示例，不执行命令）",
            blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
    except SlackApiError as exc:
        print(json.dumps({"ok": False, "error": exc.response.get("error")}, ensure_ascii=False))
        return 1
    blocks = (response.get("message") or {}).get("blocks") or []
    print(json.dumps({"ok": True, "ts": response["ts"], "rendered_blocks": blocks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
