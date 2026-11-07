"""Durable shared/exclusive admission for memory-heavy host work."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from openhands_factory.state import atomic_write_json, read_json


def _is_gate_payload(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("shared"), list)
        and (value.get("exclusive") is None or isinstance(value.get("exclusive"), dict))
        and isinstance(value.get("priority", []), list)
    )


PRIORITY_DEMAND_SECONDS = 600


def instance_priority() -> str:
    """Host admission priority for this daemon: ``high`` or ``normal``."""

    value = os.environ.get("FACTORY_INSTANCE_PRIORITY", "normal").strip().lower()
    return "high" if value == "high" else "normal"


def reserved_priority_slots() -> int:
    try:
        return max(int(os.environ.get("FACTORY_PRIORITY_RESERVED_SLOTS", "1")), 0)
    except ValueError:
        return 1


class HostResourceGate:
    """Coordinate bounded agents and exclusive verification across daemons."""

    def __init__(
        self,
        path: Path,
        shared_limit: int,
        lease_seconds: int = 7_500,
        *,
        priority: str | None = None,
        reserved_slots: int | None = None,
    ) -> None:
        if shared_limit <= 0:
            raise ValueError("shared_limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.path = path
        self.lock = FileLock(str(path) + ".lock")
        self.shared_limit = shared_limit
        self.lease_seconds = lease_seconds
        # A high-priority daemon (e.g. Neptune) registers demand on every
        # admission attempt. While that demand is fresh, normal daemons leave
        # `reserved_slots` provider slots free for it; idle demand expires.
        self.priority = priority if priority is not None else instance_priority()
        self.reserved_slots = (
            reserved_slots if reserved_slots is not None else reserved_priority_slots()
        )

    @staticmethod
    def _pid_is_live(pid: object) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _entry_is_active(self, entry: object, now: datetime) -> bool:
        if not isinstance(entry, dict) or not self._pid_is_live(entry.get("pid")):
            return False
        expires_at = entry.get("expires_at")
        if not isinstance(expires_at, str):
            return False
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
        return expiry.tzinfo is not None and now < expiry

    def _load_active(self) -> dict[str, object]:
        payload = read_json(
            self.path,
            {"shared": [], "exclusive": None},
            validator=_is_gate_payload,
        )
        now = datetime.now(UTC)
        shared = payload.get("shared", [])
        exclusive = payload.get("exclusive")
        priority = payload.get("priority", [])
        return {
            "shared": [entry for entry in shared if self._entry_is_active(entry, now)]
            if isinstance(shared, list)
            else [],
            "exclusive": exclusive if self._entry_is_active(exclusive, now) else None,
            "priority": [entry for entry in priority if self._entry_is_active(entry, now)]
            if isinstance(priority, list)
            else [],
        }

    def _register_priority_demand(self, payload: dict[str, object]) -> None:
        current = payload["priority"]
        demand = [
            entry
            for entry in (current if isinstance(current, list) else [])
            if isinstance(entry, dict) and entry.get("pid") != os.getpid()
        ]
        demand.append(
            {
                "owner": f"priority:{os.getpid()}",
                "pid": os.getpid(),
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=PRIORITY_DEMAND_SECONDS)
                ).isoformat(),
            }
        )
        payload["priority"] = demand

    def _admission_limit(self, payload: dict[str, object]) -> int:
        """Slots this daemon may fill, after any reservation for priority demand."""

        if self.priority == "high" or not payload["priority"]:
            return self.shared_limit
        return max(self.shared_limit - self.reserved_slots, 1)

    def _save(self, payload: dict[str, object]) -> None:
        atomic_write_json(self.path, payload, validator=_is_gate_payload)

    def _entry(self, owner: str) -> dict[str, object]:
        return {
            "owner": owner,
            "pid": os.getpid(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat(),
        }

    def acquire_shared(self, owner: str) -> bool:
        """Acquire one provider slot without overtaking a waiting verifier."""

        with self.lock:
            payload = self._load_active()
            shared = payload["shared"]
            if payload["exclusive"] is not None or not isinstance(shared, list):
                self._save(payload)
                return False
            if self.priority == "high":
                self._register_priority_demand(payload)
            if len(shared) >= self._admission_limit(payload):
                self._save(payload)
                return False
            shared.append(self._entry(owner))
            self._save(payload)
            return True

    def available_shared_slots(self) -> int:
        """Return current reader capacity without mutating durable admission."""

        with self.lock:
            payload = self._load_active()
            shared = payload["shared"]
            if payload["exclusive"] is not None or not isinstance(shared, list):
                return 0
            return max(self._admission_limit(payload) - len(shared), 0)

    def release_shared(self, owner: str) -> None:
        with self.lock:
            payload = self._load_active()
            shared = payload["shared"]
            if isinstance(shared, list):
                payload["shared"] = [
                    entry
                    for entry in shared
                    if not (isinstance(entry, dict) and entry.get("owner") == owner)
                ]
            self._save(payload)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Reserve writer priority, drain providers, and exclude both daemons."""

        owner = f"exclusive:{os.getpid()}:{uuid4().hex}"
        try:
            # Admission already owns writer priority while readers drain. A
            # timeout/cancellation here needs the same fenced cleanup as the
            # held body, even though the context manager has not yielded yet.
            while True:
                with self.lock:
                    payload = self._load_active()
                    exclusive = payload["exclusive"]
                    if exclusive is None or (
                        isinstance(exclusive, dict) and exclusive.get("owner") == owner
                    ):
                        payload["exclusive"] = self._entry(owner)
                        self._save(payload)
                        shared = payload["shared"]
                        if isinstance(shared, list) and not shared:
                            break
                    else:
                        self._save(payload)
                time.sleep(0.1)
            yield
        finally:
            with self.lock:
                payload = self._load_active()
                exclusive = payload["exclusive"]
                if isinstance(exclusive, dict) and exclusive.get("owner") == owner:
                    payload["exclusive"] = None
                self._save(payload)
