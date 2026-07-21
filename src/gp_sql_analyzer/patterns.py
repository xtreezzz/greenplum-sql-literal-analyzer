from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PatternInfo:
    family: str
    regex_features: dict[str, Any] = field(default_factory=dict)

    @property
    def format_signature(self) -> str:
        enabled = [name for name, value in sorted(self.regex_features.items()) if value]
        return "+".join(enabled) if enabled else self.family


def _unescaped_wildcards(pattern: str) -> list[tuple[int, str]]:
    wildcards: list[tuple[int, str]] = []
    escaped = False
    for index, character in enumerate(pattern):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character in {"%", "_"}:
            wildcards.append((index, character))
    return wildcards


def _regex_features(pattern: str, *, case_insensitive: bool) -> dict[str, bool]:
    return {
        "anchored_start": pattern.startswith("^") or pattern.startswith(r"\A"),
        "anchored_end": pattern.endswith("$") or pattern.endswith(r"\Z"),
        "groups": bool(re.search(r"(?<!\\)\(", pattern)),
        "character_classes": bool(re.search(r"(?<!\\)\[", pattern)),
        "quantifiers": bool(re.search(r"(?<!\\)(?:[*+?]|\{\d+(?:,\d*)?\})", pattern)),
        "alternation": bool(re.search(r"(?<!\\)\|", pattern)),
        "inline_flags": bool(re.search(r"\(\?[aiLmsux-]+(?::|\))", pattern)),
        "lookaround": any(marker in pattern for marker in ("(?=", "(?!", "(?<=", "(?<!")),
        "backreference": bool(re.search(r"\\(?:[1-9]|g<[^>]+>)", pattern)),
        "case_insensitive": case_insensitive,
    }


def classify_pattern(operator: str, raw_value: str) -> PatternInfo:
    normalized = " ".join(operator.upper().split())

    if "LIKE" in normalized and "REGEXP" not in normalized and "SIMILAR" not in normalized:
        wildcards = _unescaped_wildcards(raw_value)
        if not wildcards:
            family = "like_exact"
        elif wildcards == [(len(raw_value) - 1, "%")]:
            family = "like_prefix"
        elif wildcards == [(0, "%")]:
            family = "like_suffix"
        elif wildcards == [(0, "%"), (len(raw_value) - 1, "%")]:
            family = "like_contains"
        else:
            family = "like_complex"
        return PatternInfo(family=family)

    if normalized in {"~", "~*", "!~", "!~*"} or "REGEXP" in normalized:
        return PatternInfo(
            family="regex",
            regex_features=_regex_features(
                raw_value,
                case_insensitive=normalized in {"~*", "!~*"} or normalized.endswith("_I"),
            ),
        )

    if "SIMILAR TO" in normalized:
        return PatternInfo(
            family="similar_to",
            regex_features=_regex_features(raw_value, case_insensitive=False),
        )

    return PatternInfo(family="exact_value")
