"""Per-project operator controls that deploys never overwrite.

The updater pauses and resumes a project through its ``control.json`` while it
drains for a deploy. Operator intent (from the web status page or CLI) lives in
``FACTORY_OPERATOR_CONTROL_DIR/<owner>__<repo>.json`` instead, so a deploy can't
silently undo an operator pause and an operator resume can't cut a drain short.

``{"paused": bool, "priority": "high" | "normal" | null}``; a missing or
malformed file means no override.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_OPERATOR_CONTROL_DIR = Path("/var/lib/repo-factory/operator")
PRIORITIES = frozenset({"high", "normal"})
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def operator_control_dir() -> Path:
    return Path(os.environ.get("FACTORY_OPERATOR_CONTROL_DIR", str(DEFAULT_OPERATOR_CONTROL_DIR)))


def control_path(repository: str, directory: Path | None = None) -> Path:
    if not _SLUG.fullmatch(repository):
        raise ValueError("repository must be owner/name")
    return (directory or operator_control_dir()) / (
        repository.casefold().replace("/", "__") + ".json"
    )


def read_controls(repository: str | None, directory: Path | None = None) -> dict[str, Any]:
    if not repository:
        return {}
    try:
        value = json.loads(control_path(repository, directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    if isinstance(value.get("paused"), bool):
        result["paused"] = value["paused"]
    if value.get("priority") in PRIORITIES:
        result["priority"] = value["priority"]
    return result


def write_controls(
    repository: str, updates: dict[str, Any], directory: Path | None = None
) -> dict[str, Any]:
    """Merge validated updates into the project's operator file atomically."""

    current = read_controls(repository, directory)
    for key, value in updates.items():
        if key == "paused" and isinstance(value, bool):
            current["paused"] = value
        elif key == "priority" and value in PRIORITIES:
            current["priority"] = value
        else:
            raise ValueError(f"unsupported operator control: {key}")
    path = control_path(repository, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(current, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return current


def operator_paused(repository: str | None) -> bool:
    return bool(read_controls(repository).get("paused", False))


def operator_priority(repository: str | None) -> str | None:
    priority = read_controls(repository).get("priority")
    return priority if isinstance(priority, str) else None
