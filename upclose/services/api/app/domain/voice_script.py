"""Spoken script for the paid report audio. Pure: no network, ORM or framework imports.

The script is built only from narration statements already frozen and validated; this
module re-words them for speech (short sentences, numbers read naturally), prefixes
simulated-source content, caps the length, and always ends with the disclosure line.
"""

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SIMULATED_PREFIX = "Simulated: "


class VoiceScriptError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceConfig:
    script_version: str
    max_chars: int
    disclosure: str


@dataclass(frozen=True)
class VoiceLine:
    text: str
    simulated: bool = False


class _HasText(Protocol):
    text: str


def load_voice_config(path: str | Path) -> VoiceConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    config = VoiceConfig(script_version=str(raw["script_version"]), max_chars=int(raw["max_chars"]),
                         disclosure=str(raw["disclosure"]).strip())
    if not config.disclosure:
        raise VoiceScriptError("voice config requires a disclosure line")
    if config.max_chars <= len(config.disclosure) + 1:
        raise VoiceScriptError("max_chars must leave room beyond the disclosure line")
    return config


_DOLLARS = re.compile(r"\$(\d[\d,]*)(?:\.(\d{2}))?")
_DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d.])")
_SPACES = re.compile(r"\s+")


def speak_numbers(text: str) -> str:
    """'$1,200' -> '1,200 dollars'; '7.5' -> '7 point 5'. Digits are never changed."""
    def money(m: re.Match[str]) -> str:
        cents = m.group(2)
        extra = f" and {int(cents)} cents" if cents and cents != "00" else ""
        return f"{m.group(1)} dollars{extra}"
    out = _DOLLARS.sub(money, text)
    out = _DECIMAL.sub(lambda m: f"{m.group(1)} point {' '.join(m.group(2))}", out)
    return _SPACES.sub(" ", out.replace("(", ", ").replace(")", ",")).replace(" ,", ",").strip()


def _sentence(text: str) -> str:
    spoken = speak_numbers(text)
    return spoken if spoken[-1:] in (".", "!", "?") else spoken + "."


def build_script(lines: Iterable[VoiceLine | _HasText | str], config: VoiceConfig, *, simulated: bool = False) -> str:
    """Join statements into a spoken script of at most `config.max_chars` characters.

    Whole statements are dropped from the end when the cap would be exceeded; the
    disclosure line is always present as the last sentence.
    """
    sentences: list[str] = []
    for item in lines:
        if isinstance(item, str):
            item = VoiceLine(item, simulated)
        elif not isinstance(item, VoiceLine):
            item = VoiceLine(item.text, simulated)
        if not item.text.strip():
            continue
        body = _sentence(item.text)
        sentences.append(SIMULATED_PREFIX + body if item.simulated else body)
    budget = config.max_chars - len(config.disclosure) - 1
    kept: list[str] = []
    used = 0
    for sentence in sentences:
        cost = len(sentence) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(sentence)
        used += cost
    kept.append(config.disclosure)
    script = " ".join(kept)
    if len(script) > config.max_chars:  # unreachable by construction; fail closed
        raise VoiceScriptError("script exceeds character cap")
    return script


def lines_from_json(raw: object) -> tuple[VoiceLine, ...]:
    """Accept a list of statements or {"statements": [...]}; items are strings or {"text", "simulated"?}."""
    if isinstance(raw, dict):
        raw = raw.get("statements")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise VoiceScriptError("narration JSON must be a list of statements or an object with 'statements'")
    out: list[VoiceLine] = []
    for item in raw:
        if isinstance(item, str):
            out.append(VoiceLine(item))
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            out.append(VoiceLine(item["text"], bool(item.get("simulated", False))))
        else:
            raise VoiceScriptError("each statement needs a text string")
    return tuple(out)
