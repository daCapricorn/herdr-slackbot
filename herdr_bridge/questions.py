"""Codex async questions: read structured session evidence, never infer them from prose.

Verified against Codex CLI 0.162.1. Replies use the same question identity and
envelope as its desktop and terminal UI. This module does not write to Codex state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from .events import session_of
from .codex_session import resolve_session, SessionUnavailable


class QuestionUnavailable(RuntimeError):
    pass


def question_id(message_id, index):
    return json.dumps(["request_user_input_async", message_id, index], ensure_ascii=False, separators=(",", ":"))


def fingerprint(question):
    return hashlib.sha256(json.dumps({k: question[k] for k in ("id", "title", "options", "thread_id", "turn_id")},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def reply_text(question, answer):
    # The TUI bounds the model-authored question framing to 512 UTF-8 bytes.
    title = question["title"].encode()[:512].decode("utf-8", errors="ignore").replace("\n", " ").replace("\r", " ")
    body = json.dumps([{"questionItemId": question["id"], "question": title, "answer": answer}],
                      ensure_ascii=False, separators=(",", ":"))
    return "<send_user_message_question_reply>\n" + body + "\n</send_user_message_question_reply>"


def replies(text):
    text = text.strip()
    if text.startswith("# Context from my IDE setup:\n"):
        text = text.rsplit("\n## My request for Codex:\n", 1)[-1].strip()
    start, end = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
    if not text.startswith(start) or not text.endswith(end):
        return []
    try:
        data = json.loads(text[len(start):-len(end)])
        data = data if isinstance(data, list) else [data]
        return [(d["questionItemId"], d["answer"]) for d in data
                if isinstance(d, dict) and isinstance(d.get("questionItemId"), str) and isinstance(d.get("answer"), str)]
    except (ValueError, TypeError):
        return []


class _Rollout:
    def __init__(self, path, thread_id, cwd):
        self.path, self.thread_id, self.cwd = path, thread_id, cwd
        self.reset()

    def reset(self):
        self.offset, self.inode = 0, None
        self.valid, self.turn_id = False, None
        self.pending, self.answers = {}, {}

    def read(self):
        stat = self.path.stat()
        inode = (stat.st_dev, stat.st_ino)
        if self.inode != inode or stat.st_size < self.offset:
            self.reset()
        self.inode = inode
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break  # A concurrent writer's partial record is not evidence.
                self.offset = stream.tell()
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                self.apply(row)
        if not self.valid:
            raise QuestionUnavailable("Codex session identity is unavailable")
        return {"thread_id": self.thread_id, "turn_id": self.turn_id,
                "pending": dict(self.pending), "answers": dict(self.answers)}

    def apply(self, row):
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            return
        payload = row.get("payload", {})
        kind = row.get("type")
        if kind == "session_meta":
            self.valid = payload.get("id") == self.thread_id and payload.get("cwd") == self.cwd
        if not self.valid:
            return
        if kind == "event_msg":
            event = payload.get("type")
            if event == "task_started":
                self.turn_id = payload.get("turn_id")
                self.pending.clear()
            elif event in ("task_complete", "turn_aborted") and payload.get("turn_id") == self.turn_id:
                self.turn_id = None
                self.pending.clear()
            elif event == "item_completed" and self.turn_id and payload.get("turn_id") == self.turn_id:
                item = payload.get("item", {})
                if not isinstance(item, dict) or item.get("type") != "AgentMessage" or item.get("delivery") != "async":
                    return
                message_id, questions = item.get("id"), item.get("questions")
                if not isinstance(message_id, str) or not isinstance(questions, list):
                    return
                for index, q in enumerate(questions[:32]):
                    if not isinstance(q, dict) or not isinstance(q.get("title"), str) or not q["title"].strip():
                        continue
                    options = q.get("options") or []
                    if not isinstance(options, list) or any(not isinstance(o, str) or not o.strip() for o in options):
                        continue
                    # Match the TUI's bounded suggestion list.
                    options = [o for o in options[:32] if len(o.encode()) <= 512]
                    qid = question_id(message_id, index)
                    if len(qid.encode()) > 512:
                        continue
                    if qid not in self.answers:
                        self.pending[qid] = {"id": qid, "title": q["title"], "options": options,
                                             "thread_id": self.thread_id, "turn_id": self.turn_id}
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content", [])
            if not isinstance(content, list):
                return
            text = "".join(c["text"] for c in content if isinstance(c, dict)
                           and c.get("type") == "input_text" and isinstance(c.get("text"), str))
            for qid, answer in replies(text):
                self.answers[qid] = answer
                self.pending.pop(qid, None)


class CodexQuestionSource:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.cache = {}
        self.lock = threading.RLock()

    def snapshot(self, info, *, unique_workspace=False):
        with self.lock:
            return self._snapshot(info, unique_workspace=unique_workspace)

    def _snapshot(self, info, *, unique_workspace=False):
        if info.get("agent") != "codex" or not info.get("cwd"):
            raise QuestionUnavailable("An exact Codex workspace is required")
        try:
            tid, rollout, cwd = resolve_session(info, home=self.home, unique_workspace=unique_workspace)
            path = str(rollout)
            key = (tid, path, cwd)
            if key not in self.cache:
                if len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = _Rollout(Path(path), tid, cwd)
            return self.cache[key].read()
        except SessionUnavailable as exc:
            raise QuestionUnavailable(str(exc)) from exc
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise QuestionUnavailable("Cannot read Codex question evidence") from exc


def input_plan(screen, question, option, answer, pending_ids):
    """Use exact visible question choices, or the empty main composer, never a raw keypad."""
    lines = [line.rstrip() for line in screen.splitlines()]
    if any("Would you like to run the following command?" in line for line in lines[-35:]):
        raise QuestionUnavailable("A command approval is open; nothing was sent")
    # An expanded question menu has a progress header and its exact title/choices.
    for start in reversed([i for i, line in enumerate(lines) if re.fullmatch(r"\s*\d+ of \d+\s*", line)]):
        progress = [int(n) for n in re.findall(r"\d+", lines[start])]
        if progress != [pending_ids.index(question["id"]) + 1, len(pending_ids)]:
            continue
        region = lines[start + 1:]
        footer = next((i for i, line in enumerate(region) if "submit" in line and "skip" in line), None)
        if footer is None or option is None:
            continue
        after = region[footer + 1:]
        if len(after) > 12 or any(re.match(r"^\s*›(?:\s|$)", line) for line in after):
            continue  # A quoted/historical menu cannot authorize input into the composer.
        rows = [(i, m) for i, line in enumerate(region[:footer])
                if (m := re.match(r"^\s*([› ]?)\s*(\d+)\.\s+(.*)$", line))]
        if not rows or rows[0][1].group(2) != "1":
            continue
        title = " ".join(line.strip() for line in region[:rows[0][0]] if line.strip())
        if title != " ".join(question["title"].split()):
            continue
        labels = []
        for k, (i, m) in enumerate(rows):
            end = rows[k + 1][0] if k + 1 < len(rows) else footer
            labels.append(" ".join([m.group(3).strip(), *(l.strip() for l in region[i + 1:end] if l.strip())]))
        expected = [" ".join(o.split()) for o in question["options"]]
        if labels[:len(expected)] != expected or len(labels) != len(expected) + 1 or not labels[-1].startswith("Other"):
            continue
        if not 0 <= option < len(expected) or option >= 9:
            raise QuestionUnavailable("This option needs the main composer; nothing was sent")
        return {"mode": "keys", "keys": [str(option + 1)], "answer": answer}
    # A collapsed pending-question summary and known empty composer are mandatory.
    # Unknown placeholders/drafts/modal layouts fail closed rather than append input.
    prompts = [i for i in range(max(0, len(lines) - 12), len(lines)) if re.match(r"^\s*›(?:\s|$)", lines[i])]
    empty = bool(prompts) and lines[prompts[-1]].strip() in ("›", "› Ask Codex to do anything")
    # Wrapped draft content cannot share the placeholder's first line.
    empty = empty and (prompts[-1] + 1 == len(lines) or not lines[prompts[-1] + 1].strip())
    if (any(re.fullmatch(rf"\s*\? {len(pending_ids)} questions?(?: · \d+s)?\s*", line) for line in lines[-35:])
            and empty
            and not any(re.fullmatch(r"\s*\d+ of \d+\s*", line) for line in lines[-35:])):
        return {"mode": "prompt", "keys": [], "answer": answer, "text": reply_text(question, answer)}
    raise QuestionUnavailable("Question input is not ready or the composer contains a draft; nothing was sent")
