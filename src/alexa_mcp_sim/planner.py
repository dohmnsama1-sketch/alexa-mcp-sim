"""Planners turn an utterance into MCP tool calls.

InteractionModelPlanner is deterministic and offline: intents with sample utterances and {slots}, mapped to a
tool and an argument template. Templates can use {speaker}, {slot_name} and {last.<tool>.<path>} (a value from
the most recent result of a tool in this conversation, which is how "yes, order it" finds the quote code).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WAKE = re.compile(r"^\s*(hey\s+)?alexa[\s,]*", re.I)


def normalize(text: str) -> str:
    text = WAKE.sub("", text.strip())
    text = text.replace("’", "'")
    text = re.sub(r"[^\w\s':-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    intent: str = ""


@dataclass
class Conversation:
    speaker: str = ""
    last: dict[str, Any] = field(default_factory=dict)  # tool name -> structuredContent of its last result
    history: list[dict[str, Any]] = field(default_factory=list)

    def remember(self, tool: str, structured: Any) -> None:
        if structured is not None:
            self.last[tool] = structured


@dataclass
class Plan:
    calls: list[Call]
    say: str | None = None  # a reply that needs no tool (e.g. "I didn't catch that")


class InteractionModelPlanner:
    name = "interaction-model"

    def __init__(self, model: dict[str, Any]):
        self.raw = model
        self.fallback = model.get("fallback", "Sorry, I can't help with that yet.")
        self.intents = []
        for intent in model["intents"]:
            for sample in intent["samples"]:
                self.intents.append((self._compile(sample), len(re.sub(r"\{\w+\}", "", sample)), intent))
        # longer literal text first = more specific sample wins
        self.intents.sort(key=lambda x: -x[1])

    @classmethod
    def from_file(cls, path: str | Path) -> "InteractionModelPlanner":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def _compile(sample: str) -> re.Pattern:
        marked = re.sub(r"\{(\w+)\}", lambda m: f" qqslot{m.group(1)}qq ", sample)
        rx = re.escape(normalize(marked)).replace(r"\ ", " ")
        rx = re.sub(r"qqslot(\w+?)qq", lambda m: f"(?P<{m.group(1)}>.+?)", rx)
        rx = rx.replace(" ", r"\s+")
        return re.compile(rf"^(?:please\s+)?{rx}(?:\s+please)?$", re.I)

    def plan(self, utterance: str, convo: Conversation) -> Plan:
        text = normalize(utterance)
        for rx, _, intent in self.intents:
            m = rx.match(text)
            if not m:
                continue
            slots = {k: v.strip() for k, v in m.groupdict().items()}
            if "reply" in intent:
                return Plan([], say=intent["reply"])
            missing = [v for v in re.findall(r"\{last\.([\w.]+)\}", json.dumps(intent.get("args", {})))
                       if _lookup(convo.last, v) is None]
            if missing:
                return Plan([], say=intent.get("needs_context", self.fallback))
            args = _fill(intent.get("args", {}), slots, convo)
            return Plan([Call(intent["tool"], args, intent["name"])])
        return Plan([], say=self.fallback)


def _lookup(last: dict[str, Any], path: str) -> Any:
    cur: Any = last
    for key in path.split("."):
        if isinstance(cur, list) and key.isdigit():
            if int(key) >= len(cur):
                return None
            cur = cur[int(key)]
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _fill(template: Any, slots: dict[str, str], convo: Conversation) -> Any:
    if isinstance(template, dict):
        return {k: _fill(v, slots, convo) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, slots, convo) for v in template]
    if not isinstance(template, str):
        return template
    whole = re.fullmatch(r"\{([\w.]+)\}", template)
    if whole:  # keep non-string types for whole-value placeholders
        return _resolve(whole.group(1), slots, convo)
    return re.sub(r"\{([\w.]+)\}", lambda m: str(_resolve(m.group(1), slots, convo)), template)


def _resolve(name: str, slots: dict[str, str], convo: Conversation) -> Any:
    if name == "speaker":
        return convo.speaker
    if name.startswith("last."):
        return _lookup(convo.last, name[5:])
    return slots.get(name, "")
