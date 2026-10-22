"""Rendering helpers: values, markdown tables and prose.

Everything the browser renders as markdown goes through this module, so the
escaping rules live in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MISSING = "—"


@dataclass(frozen=True)
class OutputColumn:
    """One column of a result set: how to find it, label it and format it."""

    key: str
    label: str
    unit: str = "text"


def fmt_value(value: Any, unit: str = "text") -> str:
    """Render a single cell according to its unit."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "Yes" if value else "No"

    try:
        if unit == "money":
            return f"${float(value):,.2f}"
        if unit == "money0":
            return f"${float(value):,.0f}"
        if unit == "percent":
            return f"{float(value):,.1f}%"
        if unit == "count":
            return f"{int(round(float(value))):,}"
        if unit == "number":
            return f"{float(value):,.2f}"
        if unit == "num1":
            return f"{float(value):,.1f}"
        if unit == "date":
            if hasattr(value, "strftime"):
                return value.strftime("%Y-%m-%d")
            return str(value)[:10]
        if unit == "month":
            if hasattr(value, "strftime"):
                return value.strftime("%Y-%m")
            return str(value)[:7]
    except (TypeError, ValueError):
        return str(value)

    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return str(value)


def md_escape(text: Any) -> str:
    """Escape the characters that would otherwise break a markdown table."""
    return (
        str(text if text is not None else MISSING)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def markdown_table(
    columns: Sequence[OutputColumn],
    rows: Iterable[Dict[str, Any]],
    max_rows: Optional[int] = None,
) -> str:
    """Render a GitHub-flavoured markdown table."""
    rows = list(rows)
    if max_rows is not None:
        rows = rows[:max_rows]
    if not columns:
        return ""

    header = "| " + " | ".join(md_escape(c.label) for c in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        cells = [md_escape(fmt_value(row.get(c.key), c.unit)) for c in columns]
        body.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, divider, *body])


def bullets(items: Iterable[str]) -> str:
    """Render a bullet list, skipping empties."""
    lines = [f"- {item}" for item in items if item and str(item).strip()]
    return "\n".join(lines)


def compact_number(value: Any) -> str:
    """`1234567` -> `1.2M` - used for axis labels where space is tight."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    magnitude = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            scaled = number / limit
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if magnitude >= 100:
        return f"{number:,.0f}"
    if float(number).is_integer():
        return str(int(number))
    return f"{number:,.2f}"


def humanise_unit(unit: str) -> str:
    return {
        "money": "value",
        "money0": "value",
        "percent": "share",
        "count": "count",
        "number": "value",
        "num1": "value",
        "date": "date",
        "month": "period",
    }.get(unit, "value")


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count:,} {plural_form or singular + 's'}"


def truncate(text: str, limit: int = 42) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def first_sentences(text: str, count: int = 2) -> str:
    """Keep the first ``count`` sentences of a block of prose."""
    parts: List[str] = []
    buffer = ""
    for chunk in str(text or "").split("."):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.append(chunk + ".")
        if len(parts) >= count:
            break
        buffer = chunk
    return " ".join(parts) if parts else buffer
