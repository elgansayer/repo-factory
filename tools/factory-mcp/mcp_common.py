"""Shared MCP output shaping: field projection, TOON serialisation, strict pagination, output spill.

Every list-returning tool goes through `respond()`:
  * rows are projected to the requested `fields` (default: tool's compact default set)
  * paged at PAGE_SIZE (15) items with an explicit `has_more` flag
  * serialised as TOON (tabular, header once) instead of repeated JSON keys
Large free-text results go through `spill()`: the body is written to a workspace temp file and
only a 3 line summary plus the file reference enters the conversation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

PAGE_SIZE = 15
SPILL_MAX_LINES = 40
SPILL_MAX_CHARS = 3000


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\n", "\\n")
    if any(c in text for c in (",", '"')) or text != text.strip():
        return json.dumps(text)
    return text


def to_toon(name: str, rows: list[dict[str, Any]], fields: list[str] | None = None) -> str:
    """TOON tabular array: `name[N]{a,b}:` then one comma-joined row per line."""
    if not rows:
        return f"{name}[0]:"
    cols = fields or list(rows[0].keys())
    lines = [f"{name}[{len(rows)}]{{{','.join(cols)}}}:"]
    lines.extend("  " + ",".join(_cell(r.get(c)) for c in cols) for r in rows)
    return "\n".join(lines)


def project(rows: list[dict[str, Any]], fields: str | list[str] | None, default: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    wanted = [f for f in (fields or default) if rows and f in rows[0]] or default
    return [{k: r.get(k) for k in wanted} for r in rows], wanted


def respond(
    name: str,
    rows: list[dict[str, Any]],
    *,
    default_fields: list[str],
    fields: str | list[str] | None = None,
    page: int = 1,
    fmt: str = "toon",
    header: str = "",
) -> str:
    page = max(1, int(page))
    total = len(rows)
    start = (page - 1) * PAGE_SIZE
    window = rows[start : start + PAGE_SIZE]
    has_more = start + PAGE_SIZE < total
    projected, cols = project(window, fields, default_fields)
    meta = f"page={page} size={len(window)} total={total} has_more={'true' if has_more else 'false'}"
    body = json.dumps(projected, separators=(",", ":")) if fmt == "json" else to_toon(name, projected, cols)
    return "\n".join(x for x in (header, meta, body) if x)


def spill_dir() -> Path:
    base = os.environ.get("FACTORY_MCP_SPILL_DIR")
    path = Path(base) if base else Path.cwd() / ".mcp-out"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path("/tmp/mcp-out")
        path.mkdir(parents=True, exist_ok=True)
    return path


def spill(text: str, label: str, *, max_lines: int = SPILL_MAX_LINES, max_chars: int = SPILL_MAX_CHARS) -> str:
    """Return text unchanged if small, else write to a temp file and return a 3 line summary + ref."""
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
    target = spill_dir() / f"{label}-{digest}.txt"
    target.write_text(text, encoding="utf-8")
    first = lines[0][:140] if lines else ""
    last = lines[-1][:140] if lines else ""
    return (
        f"[spilled] {label}: {len(lines)} lines, {len(text)} chars -> {target}\n"
        f"first: {first}\n"
        f"last: {last}  (read ranges of the file; do not cat it)"
    )
