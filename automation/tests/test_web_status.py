from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from openhands_factory.web_status import collect_status, read_env_file, render_html


def test_status_combines_instances_without_job_bodies_or_secrets(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    instances.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    (instances / "neptune.env").write_text(
        f"FACTORY_STATE_DIR={state}\nFACTORY_INSTANCE_PRIORITY=high\nDEEPSEEK_API_KEY=secret\n"
    )
    now = datetime.now(UTC).isoformat()
    (state / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "state": "done",
                        "updated_at": now,
                        "task": {"identifier": "1", "title": "<b>x</b>", "body": "private body"},
                    },
                    {"state": "implementing", "updated_at": now, "task": {"identifier": "2"}},
                ]
            }
        )
    )
    (state / "daemon.json").write_text(json.dumps({"status": "running", "active_jobs": ["2"]}))
    (shared / "agent_health.json").write_text(
        json.dumps({"breakers": [{"provider": "claude", "state": "closed"}]})
    )

    status = collect_status(instances, shared)
    [item] = status["instances"]
    assert item["priority"] == "high"
    assert item["in_flight"] == 1
    assert item["done_last_24h"] == 1
    page = render_html(status)
    rendered = json.dumps(status) + page
    assert "private body" not in rendered
    assert "secret" not in rendered
    assert "&lt;b&gt;x&lt;/b&gt;" in page


def test_env_reader_ignores_comments(tmp_path: Path) -> None:
    path = tmp_path / "a.env"
    path.write_text("# c\nA='1'\n\nB=2\n")
    assert read_env_file(path) == {"A": "1", "B": "2"}


def test_basic_auth_is_required_when_configured(monkeypatch, tmp_path: Path) -> None:
    import base64
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from openhands_factory import web_status

    monkeypatch.setattr(web_status, "INSTANCES_DIR", tmp_path)
    monkeypatch.setattr(web_status, "SHARED_DIR", tmp_path)
    monkeypatch.setenv("FACTORY_WEB_USER", "admin")
    monkeypatch.setenv("FACTORY_WEB_PASSWORD", "pw")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web_status.StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/health") as response:
            assert response.status == 200
        try:
            urllib.request.urlopen(base + "/api/status")
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        request = urllib.request.Request(base + "/api/status")
        request.add_header("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read())["instances"] == []
    finally:
        server.shutdown()


def test_projects_table_lists_each_instance() -> None:
    from openhands_factory.web_status import render_projects_table

    status = {
        "instances": [
            {
                "name": "neptune",
                "priority": "high",
                "max_parallel_jobs": "2",
                "paused": False,
                "status": "running",
                "active_jobs": ["23"],
                "in_flight": 4,
                "done_last_24h": 1,
            }
        ]
    }
    lines = render_projects_table(status).splitlines()
    assert lines[0].startswith("PROJECT")
    assert lines[1].split() == ["neptune", "high", "2", "running", "1", "4", "1"]
