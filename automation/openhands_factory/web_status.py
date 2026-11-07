"""Read-only web status for every Factory instance on this host.

Serves one HTML page and ``/api/status`` JSON built from the instances'
durable state files. It never writes state, never shows job bodies, errors in
full or credentials, and binds to loopback unless told otherwise. Set
``FACTORY_WEB_TOKEN`` to require ``?token=`` (or ``Authorization: Bearer``).
"""

from __future__ import annotations

import hmac
import html
import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

INSTANCES_DIR = Path("/etc/repo-factory/instances")
SHARED_DIR = Path("/var/lib/repo-factory/shared")
ACTIVE_STATES = frozenset(
    {
        "implementing",
        "verifying",
        "repairing",
        "quality-repairing",
        "ci-pending",
        "reviewing",
        "merge-queued",
    }
)
RECENT_LIMIT = 15
ERROR_CHARS = 160


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _jobs(state_dir: Path) -> list[dict[str, Any]]:
    payload = _read_json(state_dir / "jobs.json")
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [job for job in jobs or [] if isinstance(job, dict)]


def _short(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = " ".join(value.split())
    return text[:ERROR_CHARS] + ("…" if len(text) > ERROR_CHARS else "")


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    raw_task = job.get("task")
    task: dict[str, Any] = raw_task if isinstance(raw_task, dict) else {}
    return {
        "id": task.get("identifier"),
        "title": _short(task.get("title")),
        "state": job.get("state"),
        "pull_request": job.get("pull_request"),
        "provider": job.get("implementation_provider"),
        "attempts": job.get("attempts"),
        "updated_at": job.get("updated_at"),
        "next_attempt_at": job.get("next_attempt_at"),
        "last_error": _short(job.get("last_error")),
    }


def _today_metrics(state_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(state_dir / "metrics-daily.json")
    days = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(days, dict):
        return {}
    today = days.get(datetime.now(UTC).date().isoformat())
    if not isinstance(today, dict):
        return {}
    providers: dict[str, dict[str, Any]] = {}
    for key, value in today.items():
        if not isinstance(value, dict):
            continue
        provider = str(key).split(":", 1)[0]
        bucket = providers.setdefault(
            provider, {"calls": 0, "successes": 0, "failures": 0, "timeouts": 0}
        )
        for field in bucket:
            number = value.get(field)
            if isinstance(number, int | float):
                bucket[field] += int(number)
    return providers


def instance_status(name: str, env: dict[str, str]) -> dict[str, Any]:
    state_dir = Path(env.get("FACTORY_STATE_DIR", f"/var/lib/repo-factory/{name}/state"))
    jobs = _jobs(state_dir)
    daemon = _read_json(state_dir / "daemon.json")
    daemon = daemon if isinstance(daemon, dict) else {}
    counts = Counter(str(job.get("state")) for job in jobs)
    day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    recent = sorted(
        (job for job in jobs if str(job.get("updated_at") or "") >= day_ago),
        key=lambda job: str(job.get("updated_at") or ""),
        reverse=True,
    )
    return {
        "name": name,
        "repository": env.get("FACTORY_GITHUB_REPOSITORY") or env.get("FACTORY_REPOSITORY_PROFILE"),
        "priority": env.get("FACTORY_INSTANCE_PRIORITY", "normal"),
        "max_parallel_jobs": env.get("FACTORY_MAX_PARALLEL_JOBS"),
        "status": daemon.get("status"),
        "paused": daemon.get("paused"),
        "storage_blocked": daemon.get("storage_blocked"),
        "updated_at": daemon.get("updated_at"),
        "runtime_version": daemon.get("runtime_version"),
        "active_jobs": daemon.get("active_jobs") or [],
        "counts": dict(counts),
        "in_flight": sum(counts[state] for state in ACTIVE_STATES),
        "done_last_24h": sum(1 for job in recent if job.get("state") == "done"),
        "recent": [_job_summary(job) for job in recent[:RECENT_LIMIT]],
        "providers_today": _today_metrics(state_dir),
    }


def _breakers(shared_dir: Path) -> list[dict[str, Any]]:
    payload = _read_json(shared_dir / "agent_health.json")
    breakers = payload.get("breakers") if isinstance(payload, dict) else None
    result = []
    for breaker in breakers or []:
        if not isinstance(breaker, dict):
            continue
        opened = breaker.get("opened_at")
        retry = breaker.get("retry_after_seconds")
        retry_at = None
        if isinstance(opened, str) and isinstance(retry, int | float):
            try:
                retry_at = (datetime.fromisoformat(opened) + timedelta(seconds=retry)).isoformat()
            except ValueError:
                retry_at = None
        result.append(
            {
                "provider": breaker.get("provider"),
                "state": breaker.get("state"),
                "last_failure_kind": breaker.get("last_failure_kind"),
                "retry_at": retry_at if breaker.get("state") != "closed" else None,
            }
        )
    return result


def _host_gate(shared_dir: Path) -> dict[str, Any]:
    payload = _read_json(shared_dir / "host-resource-gate.json")
    payload = payload if isinstance(payload, dict) else {}
    raw_shared = payload.get("shared")
    shared: list[Any] = raw_shared if isinstance(raw_shared, list) else []
    return {
        "shared": [
            str(entry.get("owner", "")).split(":")[2:4]
            for entry in shared
            if isinstance(entry, dict)
        ],
        "exclusive": payload.get("exclusive") is not None,
        "priority_demand": bool(payload.get("priority")),
    }


def collect_status(
    instances_dir: Path = INSTANCES_DIR, shared_dir: Path = SHARED_DIR
) -> dict[str, Any]:
    instances = [
        instance_status(path.stem, read_env_file(path))
        for path in sorted(instances_dir.glob("*.env"))
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "instances": instances,
        "providers": _breakers(shared_dir),
        "host": _host_gate(shared_dir),
    }


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def render_html(status: dict[str, Any]) -> str:
    rows = []
    for item in status["instances"]:
        rows.append(
            "<tr>"
            f"<td><b>{_e(item['name'])}</b><br><small>{_e(item['repository'])}</small></td>"
            f"<td>{_e(item['priority'])}</td>"
            f"<td>{_e('paused' if item['paused'] else item['status'])}"
            f"{' · storage blocked' if item['storage_blocked'] else ''}</td>"
            f"<td>{len(item['active_jobs'])}</td>"
            f"<td>{item['in_flight']}</td>"
            f"<td>{item['done_last_24h']}</td>"
            f"<td><small>{_e(item['updated_at'])}</small></td>"
            "</tr>"
        )
    providers = "".join(
        f"<tr><td>{_e(p['provider'])}</td><td class='{_e(p['state'])}'>{_e(p['state'])}</td>"
        f"<td>{_e(p['last_failure_kind'])}</td><td><small>{_e(p['retry_at'])}</small></td></tr>"
        for p in status["providers"]
    )
    host = status["host"]
    sections = []
    for item in status["instances"]:
        usage = ", ".join(
            f"{_e(name)}: {v['successes']}/{v['calls']} ok"
            for name, v in sorted(item["providers_today"].items())
        )
        jobs = "".join(
            f"<tr><td>{_e(job['id'])}</td><td>{_e(job['title'])}</td><td>{_e(job['state'])}</td>"
            f"<td>{_e(job['pull_request'])}</td><td>{_e(job['provider'])}</td>"
            f"<td><small>{_e(job['updated_at'])}</small></td>"
            f"<td><small>{_e(job['last_error'])}</small></td></tr>"
            for job in item["recent"]
        )
        sections.append(
            f"<h2>{_e(item['name'])}</h2><p>Provider calls today: {usage or 'none'}</p>"
            "<table><tr><th>Task</th><th>Title</th><th>State</th><th>PR</th><th>Provider</th>"
            f"<th>Updated</th><th>Last error</th></tr>{jobs}</table>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Factory Status</title>
<style>
:root{{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;
  --ok:#1a7f37;--bad:#b42318;--warn:#9a6700}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#999;
  --line:#333;--ok:#4ac26b;--bad:#ff7b72;--warn:#d29922}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;
  margin:0 auto;max-width:1100px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 20px;display:block;
  overflow-x:auto}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;
  vertical-align:top}}
small{{color:var(--muted)}} .closed{{color:var(--ok)}} .open{{color:var(--bad)}}
.half-open{{color:var(--warn)}}
</style></head><body>
<h1>Factory status</h1><p><small>Generated {_e(status["generated_at"])} · refreshes every 30 s ·
<a href="api/status">JSON</a></small></p>
<table><tr><th>Instance</th><th>Priority</th><th>Status</th><th>Running</th><th>Open</th>
<th>Done 24h</th><th>Heartbeat</th></tr>{"".join(rows)}</table>
<h2>Providers</h2><table><tr><th>Provider</th><th>Breaker</th><th>Last failure</th>
<th>Retry at</th></tr>
{providers}</table>
<p>Host slots in use: {len(host["shared"])}
· verifier exclusive: {"yes" if host["exclusive"] else "no"}
· priority demand: {"yes" if host["priority_demand"] else "no"}</p>
{"".join(sections)}
</body></html>"""


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "FactoryStatus/1"

    def _authorized(self) -> bool:
        token = os.environ.get("FACTORY_WEB_TOKEN")
        if not token:
            return True
        query = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        header = self.headers.get("Authorization", "")
        supplied = header.removeprefix("Bearer ") if header.startswith("Bearer ") else query
        return hmac.compare_digest(supplied.encode(), token.encode())

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, "text/plain; charset=utf-8", b"unauthorized\n")
            return
        path = urlparse(self.path).path.rstrip("/")
        status = collect_status()
        if path.endswith("/api/status"):
            self._send(200, "application/json", json.dumps(status, indent=2).encode())
        elif path in {"", "/"}:
            self._send(200, "text/html; charset=utf-8", render_html(status).encode())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(listen: str = "127.0.0.1:8787") -> None:
    host, _, port = listen.rpartition(":")
    ThreadingHTTPServer((host or "127.0.0.1", int(port)), StatusHandler).serve_forever()
