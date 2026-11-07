"""Read-only web status for every Factory instance on this host.

Serves one HTML page and ``/api/status`` JSON built from the instances'
durable state files. It never writes state, never shows job bodies, errors in
full or credentials, and binds to loopback unless told otherwise. Set
``FACTORY_WEB_TOKEN`` to require ``?token=`` (or ``Authorization: Bearer``), or
``FACTORY_WEB_USER`` and ``FACTORY_WEB_PASSWORD`` for HTTP basic auth.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import html
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from openhands_factory.operator_controls import read_controls, write_controls
from openhands_factory.operator_secrets import SECRET_KEYS, set_secret, stored_keys

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
    repository = env.get("GITHUB_REPOSITORY")
    operator = read_controls(repository)
    return {
        "name": name,
        "repository": repository or env.get("FACTORY_REPOSITORY_PROFILE"),
        "priority": operator.get("priority") or env.get("FACTORY_INSTANCE_PRIORITY", "normal"),
        "operator": operator,
        "max_parallel_jobs": env.get("FACTORY_MAX_PARALLEL_JOBS"),
        "status": daemon.get("status"),
        "paused": bool(daemon.get("paused")) or bool(operator.get("paused")),
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
    instances_dir: Path | None = None, shared_dir: Path | None = None
) -> dict[str, Any]:
    instances_dir = instances_dir or INSTANCES_DIR
    shared_dir = shared_dir or SHARED_DIR
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


def render_projects_table(status: dict[str, Any]) -> str:
    """Plain-text project overview for the ``repo-factory projects`` command."""

    header = ("PROJECT", "PRIORITY", "PARALLEL", "STATUS", "RUNNING", "OPEN", "DONE24H")
    rows = [header]
    for item in status["instances"]:
        state = "paused" if item["paused"] else str(item["status"] or "unknown")
        rows.append(
            (
                str(item["name"]),
                str(item["priority"]),
                str(item["max_parallel_jobs"] or "-"),
                state,
                str(len(item["active_jobs"])),
                str(item["in_flight"]),
                str(item["done_last_24h"]),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    )


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _actions(item: dict[str, Any]) -> str:
    name = _e(item["name"])
    operator = item.get("operator") or {}
    pause = "resume" if operator.get("paused") else "pause"
    other = "normal" if item["priority"] == "high" else "high"
    return (
        f"<button onclick=\"act('{name}','{pause}')\">{pause.title()}</button> "
        f"<button onclick=\"act('{name}','priority-{other}')\">Make {other}</button>"
    )


ACTIONS: dict[str, dict[str, Any]] = {
    "pause": {"paused": True},
    "resume": {"paused": False},
    "priority-high": {"priority": "high"},
    "priority-normal": {"priority": "normal"},
}


def apply_action(name: str, action: str, instances_dir: Path | None = None) -> dict[str, Any]:
    """Apply one operator action to a configured project, by env-file name."""

    if action not in ACTIONS:
        raise ValueError("unknown action")
    directory = instances_dir or INSTANCES_DIR
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unknown project")
    env_path = directory / f"{name}.env"
    if not env_path.is_file():
        raise ValueError("unknown project")
    repository = read_env_file(env_path).get("GITHUB_REPOSITORY")
    if not repository:
        raise ValueError("project has no GITHUB_REPOSITORY")
    return write_controls(repository, ACTIONS[action])


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
            f"<td>{_actions(item)}</td>"
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
<a href="api/status">JSON</a> · <a href="secrets">Secrets</a></small></p>
<table><tr><th>Instance</th><th>Priority</th><th>Status</th><th>Running</th><th>Open</th>
<th>Done 24h</th><th>Heartbeat</th><th>Controls</th></tr>{"".join(rows)}</table>
<h2>Providers</h2><table><tr><th>Provider</th><th>Breaker</th><th>Last failure</th>
<th>Retry at</th></tr>
{providers}</table>
<p>Host slots in use: {len(host["shared"])}
· verifier exclusive: {"yes" if host["exclusive"] else "no"}
· priority demand: {"yes" if host["priority_demand"] else "no"}</p>
{"".join(sections)}
<script>
async function act(name, action) {{
  if (!confirm(action + " " + name + "?")) return;
  const r = await fetch("api/projects/" + name + "/" + action,
    {{method: "POST", headers: {{"X-Factory-Action": "1"}}}});
  alert(r.ok ? "Done: " + action + " " + name : "Failed: " + r.status);
  location.reload();
}}
</script>
</body></html>"""


def render_secrets_html() -> str:
    stored = stored_keys()
    rows = "".join(
        f"<tr><td><code>{_e(key)}</code><br><small>{_e(label)}</small></td>"
        f"<td>{'stored' if key in stored else '—'}</td>"
        f"<td><input type='password' id='v-{_e(key)}' autocomplete='off' "
        f"placeholder='new value'> <button onclick=\"save('{_e(key)}')\">Save</button> "
        f"<button onclick=\"save('{_e(key)}', true)\">Clear</button></td></tr>"
        for key, label in SECRET_KEYS.items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Factory Secrets</title>
<style>
:root{{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;
  margin:0 auto;max-width:900px;padding:16px}}
table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}}
small{{color:var(--muted)}} input{{max-width:220px}}
</style></head><body><p><a href="./">← Status</a></p><h1>Secrets</h1>
<p>Values are stored on the host (mode 600) and used only when a key is missing from the
Factory environment. They are never shown again. Restart or redeploy to apply.</p>
<table><tr><th>Key</th><th>Stored</th><th>Set</th></tr>{rows}</table>
<script>
async function save(key, clear) {{
  const input = document.getElementById("v-" + key);
  const value = clear ? "" : input.value;
  if (!clear && !value) return;
  const r = await fetch("api/secrets/" + key, {{method: "POST",
    headers: {{"X-Factory-Action": "1", "Content-Type": "text/plain"}}, body: value}});
  input.value = "";
  alert(r.ok ? (clear ? "Cleared " : "Saved ") + key : "Failed: " + r.status);
  location.reload();
}}
</script></body></html>"""


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "FactoryStatus/1"

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        user = os.environ.get("FACTORY_WEB_USER")
        password = os.environ.get("FACTORY_WEB_PASSWORD")
        if user and password:
            if not header.startswith("Basic "):
                return False
            try:
                supplied = base64.b64decode(header[6:], validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                return False
            return hmac.compare_digest(supplied.encode(), f"{user}:{password}".encode())
        token = os.environ.get("FACTORY_WEB_TOKEN")
        if not token:
            return True
        query = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        supplied = header.removeprefix("Bearer ") if header.startswith("Bearer ") else query
        return hmac.compare_digest(supplied.encode(), token.encode())

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Factory Status"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = collect_status()
        if path.endswith("/api/status"):
            self._send(200, "application/json", json.dumps(status, indent=2).encode())
        elif path in {"", "/"}:
            self._send(200, "text/html; charset=utf-8", render_html(status).encode())
        elif path == "/secrets":
            self._send(200, "text/html; charset=utf-8", render_secrets_html().encode())
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def do_POST(self) -> None:
        configured = os.environ.get("FACTORY_WEB_TOKEN") or (
            os.environ.get("FACTORY_WEB_USER") and os.environ.get("FACTORY_WEB_PASSWORD")
        )
        # Writes need configured auth and a custom header no cross-site form can set.
        if not configured or not self._authorized():
            self._send(401, "text/plain; charset=utf-8", b"unauthorized\n")
            return
        if self.headers.get("X-Factory-Action") != "1":
            self._send(403, "text/plain; charset=utf-8", b"missing action header\n")
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "secrets"]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                self._send(413, "text/plain; charset=utf-8", b"too large\n")
                return
            value = self.rfile.read(length).decode("utf-8", "replace").strip()
            try:
                set_secret(parts[2], value or None)
            except ValueError as error:
                self._send(400, "text/plain; charset=utf-8", f"{error}\n".encode())
                return
            self._send(200, "application/json", json.dumps({"stored": bool(value)}).encode())
            return
        if len(parts) != 4 or parts[:2] != ["api", "projects"]:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")
            return
        try:
            result = apply_action(parts[2], parts[3])
        except ValueError as error:
            self._send(400, "text/plain; charset=utf-8", f"{error}\n".encode())
            return
        self._send(200, "application/json", json.dumps(result).encode())

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
