from herdr_slackbot import blocks as B
from herdr_slackbot.formatting import terminal_markdown
from herdr_slackbot.parser import extract_result


def rendered_text(blocks):
    return "\n".join((block.get("text") if isinstance(block.get("text"), str) else
                      (block.get("text") or {}).get("text", "")) or
                     "\n".join(el.get("text", "") for el in block.get("elements", [])
                               if el.get("type") == "mrkdwn") for block in blocks)


def test_terminal_history_keeps_both_roles_and_reflows_chinese_links():
    raw = (
        "› 看起来要进入thread，才能看到审批消\n"
        "  息，没有推送通知。\n"
        "• 不一定需要 @，但审批消息加 @你 更\n"
        "  合适。\n"
        "  线程通知规则 (https://\n"
        "  slack.com/\n"
        "  help/articles/115000769927-\n"
        "  Use-threads-to-organize-discussions)\n"
        "  Worked for 36s · 15:44\n"
        "› 我修改了notification的设置，再试一下\n"
        "• 已触发审批。"
    )
    blocks, cut = B.result_blocks("header", [], raw, None, markdown=False)
    rendered = rendered_text(blocks)
    assert not cut and not rendered.startswith("```")
    assert rendered.count("*👤 You*") == 2
    assert rendered.count("*🤖 Assistant*") == 2
    assert "审批消息，没有推送通知。" in rendered
    assert "[线程通知规则](https://slack.com/help/articles/115000769927-Use-threads-to-organize-discussions)" in rendered
    assert "Worked for 36s · 15:44" in rendered
    assert "我修改了notification的设置，再试一下" in rendered


def test_terminal_code_lists_and_unknown_output_keep_structure():
    raw = "• Example:\n  ```python\n  x = '**literal**'\n  print(x)\n  ```\n  • first\n  • second\n      deeply_indented()\n  $ ls -ld '/tmp'\n› 谢谢"
    out = B.terminal_mrkdwn(raw)
    assert "```\nx = '**literal**'\nprint(x)\n```" in out
    assert "• first\n• second" in out
    assert "```\n    deeply_indented()\n$ ls -ld '/tmp'\n```" in out
    assert "*👤 You*\n\n谢谢" in out
    assert terminal_markdown("fatal: unable to connect\n  details") is None
    assert B.terminal_mrkdwn("fatal: unable to connect\n  details").startswith("```\n")


def test_inline_code_stays_literal_and_agent_mentions_stay_escaped():
    out = B.to_mrkdwn("**bold** `**literal** [x](https://x.y)` <@U123> & <!channel>")
    assert out == "*bold* `**literal** [x](https://x.y)` &lt;@U123&gt; &amp; &lt;!channel&gt;"
    blocks, _ = B.result_blocks("h", [], "• <@U123> @here", None, markdown=False)
    assert blocks[-1]["type"] == "markdown"
    assert "&lt;@U123&gt;" in blocks[-1]["text"]
    assert "@\u200bhere" in blocks[-1]["text"]


def test_long_terminal_history_has_full_button_and_closed_code():
    raw = "› Earlier question\n• " + "answer " * 600 + "\n  ```\n  code\n  ```"
    blocks, cut = B.result_blocks("h", [], raw, "full-id", markdown=False, limit=120)
    assert cut
    assert blocks[-1]["elements"][0]["value"] == "full-id"
    assert all(len(b["text"]["text"]) <= 120 for b in blocks if b["type"] == "section")
    assert rendered_text(blocks).count("```") % 2 == 0


def test_preview_uses_same_conversation_format():
    out = B.last_response_blocks("› question\n• answer", markdown=False)
    assert "*👤 You*" in out[-1]["text"]["text"]
    assert "*🤖 Assistant*" in out[-1]["text"]["text"]


def test_turns_have_separate_sections_and_timing_is_metadata():
    body = "› Question one\n• Answer one\n  Worked for 3s · 10:00\n› Question two\n• Answer two"
    blocks, cut = B.result_blocks("header", [], body, None, markdown=False)
    assert not cut
    prose = [b["text"]["text"] if b["type"] == "section" else b["text"]
             for b in blocks if b["type"] in ("section", "markdown")]
    assert prose == ["header", "Question one", "Answer one", "Question two", "Answer two"]
    assert sum(b["type"] == "divider" for b in blocks) == 3
    assert "Worked for 3s · 10:00" in rendered_text(blocks)
    assert all("Worked for" not in t for t in prose)


def test_paragraphs_survive_screen_extraction_and_url_is_clickable():
    screen = "› question\n\n• First paragraph.\n\n\n  Second paragraph with https://example.com/docs.\n\n› Ask Codex to do anything\n  status"
    result = extract_result(screen, "codex")
    assert "First paragraph.\n\n  Second" in result.text
    assert "\n\n\n" not in result.text
    blocks, cut = B.result_blocks("h", [], result.text, None, markdown=False)
    assert not cut
    assert "First paragraph.\n\nSecond paragraph with https://example.com/docs." in rendered_text(blocks)
    assert "Ask Codex" not in rendered_text(blocks)


def test_role_markers_inside_code_are_literal_and_many_turns_get_full_button():
    body = "• ```\n  *👤 You*\n  literal\n  ```"
    blocks, _ = B.result_blocks("h", [], body, None, markdown=False)
    assert [b["type"] for b in blocks] == ["section", "context", "markdown"]
    assert "*👤 You*" in blocks[-1]["text"]
    body = "\n".join(f"› question {i}\n• answer {i}" for i in range(30))
    blocks, cut = B.result_blocks("h", [], body, "full", markdown=False)
    assert cut and len(blocks) <= B.MAX_BLOCKS
    assert blocks[-1]["elements"][0]["value"] == "full"
    assert "question 0" in rendered_text(blocks)


def test_ran_output_is_literal_code_until_next_turn():
    raw = ("• Ran ls -ld '/tmp'\n"
           "  └ drwxr-xr-x  3 user staff 96 /tmp\n"
           "    **literal** https://example.com\n"
           "\n"
           "  second output paragraph\n"
           "  ```not-a-real-fence\n"
           "• 目录信息已读取。\n"
           "› 下一步")
    blocks, cut = B.result_blocks("h", [], raw, None, markdown=False)
    assert not cut
    bodies = [b["text"] for b in blocks if b["type"] == "markdown"]
    tool = bodies[0]
    assert tool.startswith("```bash\nls -ld '/tmp'\n```\n```bash\n└ drwxr-xr-x")
    assert "  **literal** https://example.com" in tool
    assert "\n\nsecond output paragraph" in tool
    assert "`\u200b``not-a-real-fence" in tool
    assert tool.count("```") == 4
    assert tool.endswith("目录信息已读取。")
    assert bodies[-1] == "下一步"


def test_ran_output_closes_before_timing_and_truncated_tail_stays_code():
    text = B.terminal_mrkdwn("• Ran pwd\n  /tmp\n  Worked for 1s · 12:00")
    assert "```\n/tmp\n```\nWorked for 1s" in text
    text = B.terminal_mrkdwn("  └ raw output\n    more output\n• Done.")
    assert text.startswith("```\n  └ raw output\n    more output\n```")
    assert text.endswith("*🤖 Assistant*\n\nDone.")


def test_same_role_continues_without_dividers_or_repeated_headers():
    raw = "› first question\n› more context\n• checking\n• Ran pwd\n  /tmp\n• 完成。\n› thanks"
    blocks, cut = B.result_blocks("h", [], raw, None, markdown=False)
    assert not cut
    assert sum(b["type"] == "divider" for b in blocks) == 2
    assert rendered_text(blocks).count("*🤖 Assistant*") == 1
    bodies = [b["text"] for b in blocks if b["type"] == "markdown"]
    assert len(bodies) == 4
    assert all("\n\n\n" not in body for body in bodies)
    assert "first question" in bodies[0] and "more context" in bodies[0]
    assert bodies[1] == "checking"
    assert "```bash\npwd\n```" in bodies[2]
    assert "```bash\n/tmp\n```" in bodies[2]
    assert bodies[2].endswith("完成。")


def test_native_markdown_preserves_language_and_literal_code():
    raw = "• ```bash\n  echo '<tag> & **literal**'\n  ```"
    blocks, _ = B.result_blocks("h", [], raw, None, markdown=False)
    assert blocks[-1] == {"type": "markdown", "text": "```bash\necho '<tag> & **literal**'\n```"}


def test_output_language_follows_command_or_recognizable_content():
    raw = "• Ran ls -ld /tmp\n  └ drwxr-xr-x user staff /tmp\n• Ran curl https://example.com\n  └ {\"ok\": true}\n• Ran cat sample.py\n  import os\n  print(os.getcwd())"
    blocks, cut = B.result_blocks("h", [], raw, None, markdown=False)
    assert not cut
    text = rendered_text(blocks)
    assert "```bash\n└ drwxr-xr-x user staff /tmp\n```" in text
    assert "```json\n└ {\"ok\": true}\n```" in text
    assert "```python\nimport os\nprint(os.getcwd())\n```" in text


def test_language_inference_does_not_override_explicit_language_or_leak_to_next_reply():
    from herdr_slackbot.formatting import native_markdown
    source = "**🤖 Assistant**\nRan pwd\n```\n/tmp\n```\n**🤖 Assistant**\n```text\nimport os\n```\n```\nopaque content\n```"
    out = native_markdown(source)
    assert "```bash\n/tmp\n```" in out
    assert "```text\nimport os\n```" in out
    assert "```\nopaque content\n```" in out


def test_ran_is_bold_without_background_highlight_or_styling_code_contents():
    raw = "• Ran echo Ran\n  Ran\n• 完成。"
    blocks, cut = B.result_blocks("h", [], raw, None, markdown=False)
    assert not cut
    highlighted = [b for b in blocks if b["type"] == "rich_text"]
    assert len(highlighted) == 1
    assert highlighted[0]["elements"][0]["elements"] == [
        {"type": "text", "text": "Ran", "style": {"bold": True}},
    ]
    body = next(b["text"] for b in blocks if b["type"] == "markdown")
    assert "```bash\necho Ran\n```" in body
    assert "```bash\nRan\n```" in body
    assert body.endswith("完成。")
    assert all(b["type"] != "divider" for b in blocks)
