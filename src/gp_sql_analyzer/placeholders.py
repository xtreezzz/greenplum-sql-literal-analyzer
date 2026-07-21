from __future__ import annotations

import re
from dataclasses import dataclass


DEFAULT_PLACEHOLDER = "&CHARACTER"


@dataclass(frozen=True, slots=True)
class PlaceholderMatch:
    value: str
    start: int
    end: int
    matched: bool = True


def extract_placeholder_values(
    template_value: str,
    original_value: str,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> tuple[PlaceholderMatch, ...]:
    """Extract literal fragments represented by placeholders.

    SQL has already been parsed at this point. A regular expression is used only
    to align fixed text inside two string-literal values, never to parse SQL.
    """

    if not placeholder or placeholder not in template_value:
        return ()

    fixed_parts = template_value.split(placeholder)
    alignment = r"\A" + "(.*?)".join(re.escape(part) for part in fixed_parts) + r"\Z"
    match = re.match(alignment, original_value, flags=re.DOTALL)
    if match is None:
        return (PlaceholderMatch(original_value, 0, len(original_value), matched=False),)

    return tuple(
        PlaceholderMatch(match.group(index), match.start(index), match.end(index))
        for index in range(1, len(fixed_parts))
    )
