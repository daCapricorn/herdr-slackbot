"""Claude Code transcript record classification shared by the Slack history and the pane sync.

Record shapes relied on (besides plain prompts, tool results, interrupts and assistant messages):
- queued prompt (typed while the agent works): {"type": "attachment", "attachment":
  {"type": "queued_command", "commandMode": "prompt", "prompt": "<text>" | [blocks], "source_uuid"?}}
- background task notification: a "user" record with origin.kind "task-notification" /
  promptSource "system", text "<task-notification>...<summary>..</summary>...</task-notification>"
- compact summary: a "user" record with isCompactSummary / isVisibleInTranscriptOnly
- bash mode: "user" records whose text is <bash-input>/<bash-stdout>/<bash-stderr>
"""

from __future__ import annotations

import re

_INTERRUPT_PREFIX = "[Request interrupted"
_COMMAND_PREFIXES = ("<command-", "<local-command")
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_BASH_PREFIXES = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")
_TASK_NOTIFICATION = "<task-notification>"
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
TASK_EVENT = "Background task finished"
SYSTEM_EVENT = "System message"
COMPACT_EVENT = "Conversation compacted (earlier messages summarized)"
EVENT_MAX = 200


def _message(entry: dict) -> dict:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _blocks(entry: dict) -> list:
    content = _message(entry).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _well_formed(entry: dict) -> bool:
    if entry.get("type") not in ("user", "assistant"):
        return True
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        if block.get("type") == "text" and not isinstance(block.get("text"), str):
            return False
    return True


def _is_interrupt(entry: dict) -> bool:
    return entry.get("type") == "user" and any(
        b.get("type") == "text" and str(b.get("text", "")).startswith(_INTERRUPT_PREFIX)
        for b in _blocks(entry) if isinstance(b, dict))


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    blocks = [b for b in _blocks(entry) if isinstance(b, dict)]
    if not blocks or any(b.get("type") == "tool_result" for b in blocks):
        return False
    texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
    if any(t.startswith(_INTERRUPT_PREFIX) for t in texts):
        return False
    if texts and all(t.lstrip().startswith(_COMMAND_PREFIXES) for t in texts):
        return False
    return any(b.get("type") in ("text", "image") for b in blocks)


def _texts(entry: dict) -> list[str]:
    return [str(b.get("text", "")) for b in _blocks(entry) if b.get("type") == "text"]


def _prompt_text(blocks: list) -> str:
    """Visible text of prompt blocks: closed system-reminder blocks and command bookkeeping removed,
    images as `[image]`. If nothing is left, the text with only the reminders removed."""
    parts, raw = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image":
            parts.append("[image]")
            raw.append("[image]")
        elif kind == "text":
            text = _REMINDER_RE.sub("", str(block.get("text", ""))).strip()
            raw.append(text)
            if text and not text.startswith(_COMMAND_PREFIXES):
                parts.append(text)
    return "\n\n".join(parts) or "\n\n".join(r for r in raw if r)


def _bookkeeping_only(entry: dict) -> bool:
    blocks = [b for b in _blocks(entry) if b.get("type") in ("text", "image")]
    if any(b.get("type") == "image" for b in blocks):
        return False
    texts = [_REMINDER_RE.sub("", str(b.get("text", ""))).strip() for b in blocks]
    return all(not x or x.startswith(_COMMAND_PREFIXES) for x in texts)


def _queued_prompt(entry: dict) -> list | None:
    if entry.get("type") != "attachment" or entry.get("isMeta"):
        return None
    att = entry.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("commandMode") != "prompt":
        return None
    prompt = att.get("prompt")
    if isinstance(prompt, str):
        return [{"type": "text", "text": prompt}] if prompt.strip() else None
    if isinstance(prompt, list):
        blocks = [b for b in prompt if isinstance(b, dict) and b.get("type") in ("text", "image")]
        if any(b.get("type") == "text" and not isinstance(b.get("text"), str) for b in blocks):
            return None
        return blocks or None
    return None


def _event_text(entry: dict) -> str | None:
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return COMPACT_EVENT
    origin = entry.get("origin")
    texts = _texts(entry)
    if (isinstance(origin, dict) and origin.get("kind") == "task-notification") or \
            any(t.lstrip().startswith(_TASK_NOTIFICATION) for t in texts):
        summary = next((" ".join(m.group(1).split()) for t in texts for m in [_SUMMARY_RE.search(t)] if m), "")
        if len(summary) > EVENT_MAX:
            summary = summary[:EVENT_MAX - 1] + "…"
        return f"{TASK_EVENT}: {summary}" if summary else TASK_EVENT
    if entry.get("promptSource") == "system":
        return SYSTEM_EVENT
    return None


def _is_bash_mode(entry: dict) -> bool:
    texts = [t.lstrip() for t in _texts(entry)]
    return bool(texts) and all(t.startswith(_BASH_PREFIXES) for t in texts)


def turn_start(entry: dict) -> tuple[str, str] | None:
    """(role, text) of a record that starts a turn: ("user", prompt) for a typed or queued prompt,
    ("event", label) for a non-owner record (task notification, compact summary); else None."""
    if entry.get("isSidechain") or entry.get("isMeta"):
        return None
    queued = _queued_prompt(entry)
    if queued is not None:
        text = _prompt_text(queued)
        return None if not text or text.startswith("<send_user_message_question_reply>") else ("user", text)
    if not _well_formed(entry) or not _is_prompt(entry) or _bookkeeping_only(entry) or _is_bash_mode(entry):
        return None
    event = _event_text(entry)
    if event is not None:
        return "event", event
    text = _prompt_text(_blocks(entry))
    return None if text.startswith("<send_user_message_question_reply>") else ("user", text)
