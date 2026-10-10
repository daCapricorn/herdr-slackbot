"""Display terminal conversations without changing their stored source text."""

import ast
import json
import re


_TURN = re.compile(r"^([›❯•●])(?:\s|$)(.*)")
_ITEM = re.compile(r"^(?:[-*•]|\d+[.)]|#{1,6})\s")
_CJK = re.compile(r"[\u3400-\u9fff]")


def code_language(content: str, *, shell_output: bool = False) -> str:
    """Prefer recognizable structured content, then the execution context."""
    # Ignore the terminal output gutter only for detection, never for display.
    sample = re.sub(r"^\s*[└├│]\s?", "", content, flags=re.MULTILINE).strip()
    if sample.startswith(("{", "[")):
        try:
            if isinstance(json.loads(sample), (dict, list)):
                return "json"
        except (ValueError, RecursionError):
            pass
    if re.match(r"(?:diff --git |--- a/|@@ )", sample):
        return "diff"
    if re.match(r"(?:from \S+ import |import \S|(?:async )?def |class |print\(|if __name__)", sample):
        try:
            ast.parse(sample)
            return "python"
        except (SyntaxError, ValueError, RecursionError):
            pass
    if shell_output or sample.startswith(("$ ", "#!/bin/bash", "#!/bin/sh", "#!/usr/bin/env bash")):
        return "bash"
    return ""


def _language_fences(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    shell_output = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line in ("**👤 You**", "**🤖 Assistant**"):
            shell_output = False
        if line.startswith("Ran "):
            shell_output = True
        if line.lstrip().startswith("```"):
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith("```"):
                end += 1
            language = line.lstrip()[3:].strip()
            if not language:
                language = code_language("\n".join(lines[i + 1:end]), shell_output=shell_output)
            out.append("```" + language)
            out.extend(lines[i + 1:end])
            out.append("```")
            i = end + 1
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def native_markdown(text: str) -> str:
    """Convert terminal-rendered links without changing code or inline literals."""
    out: list[str] = []
    in_fence = False
    for line in _language_fences(text).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if line.startswith("Ran "):
            out.extend(["Ran", "", "```bash", line[4:].replace("```", "`\u200b``"), "```"])
            continue
        parts = re.split(r"(`+[^`]*`+)", line)
        for i in range(0, len(parts), 2):
            part = parts[i].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            part = re.sub(r"@(?=here\b|channel\b|everyone\b)", "@\u200b", part)
            part = re.sub(r"([^\s()\[\]<>]+)\s+\((https?://[^)\s]+)\)", r"[\1](\2)", part)
            parts[i] = part
        out.append("".join(parts))
    return "\n".join(out)


def terminal_markdown(text: str) -> str | None:
    """Recognized conversation -> Markdown; unknown terminal output stays raw.

    Keep every captured line, including earlier turns and tool output. Two spaces
    are the agent UI's continuation indent; deeper indentation stays code. Only
    continuation lines are reflowed, so independent terminal rows remain rows.
    """
    lines = text.splitlines()
    if not any(_TURN.match(line) for line in lines):
        return None
    out: list[str] = []
    in_fence = False
    indent_code = False
    in_turn = False
    tool_output = False

    def close_code() -> None:
        nonlocal indent_code
        if indent_code:
            out.append("```")
            indent_code = False

    for original in lines:
        match = _TURN.match(original) if not in_fence else None
        if match:
            close_code()
            tool_output = match[1] in "•●" and bool(re.match(r"Ran\s+\S", match[2]))
            role = "👤 You" if match[1] in "›❯" else "🤖 Assistant"
            out.extend(["", f"**{role}**", ""])
            line = match[2]
            in_turn = True
            continuation = False
        else:
            continuation = in_turn and original.startswith("  ")
            line = original[2:] if continuation else original
            if tool_output:
                if (line.strip() and not continuation) or re.match(r"\s*Worked for\b", line):
                    close_code()
                    tool_output = False
                else:
                    if line.strip() and not indent_code:
                        out.append("```")
                        indent_code = True
                    if indent_code:
                        # Tool output is literal even if it contains Markdown fences.
                        out.append(line.replace("```", "`\u200b``"))
                    elif not line.strip():
                        out.append("")
                    continue
        if line.lstrip().startswith("```"):
            close_code()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if (line.startswith("    ") or line.startswith("\t") or line.startswith("$ ")
                or line.lstrip().startswith(("└", "├", "│"))):
            if not indent_code:
                out.append("```")
                indent_code = True
            out.append(line)
            continue
        close_code()
        stripped = line.strip()
        previous = out[-1] if out else ""
        # An open parenthesized URL is often wrapped even after '/' or '-'.
        url_wrap = bool(re.search(r"\(https?://[^)]*$", previous))
        if (continuation and stripped and previous and previous != "```"
                and not _ITEM.match(stripped) and not stripped.startswith(("$ ", "|", "│", "└", "╭", "╰", "> "))
                and not re.match(r"(?:Worked|Working) for\b", stripped)
                and (url_wrap or not re.search(r"[.!?。！？：:]$", previous))):
            sep = "" if url_wrap or (_CJK.search(previous[-1:]) and _CJK.search(stripped[:1])) else " "
            out[-1] += sep + stripped
        else:
            out.append(line)
    close_code()
    if in_fence:
        out.append("```")
    return "\n".join(out).strip()
