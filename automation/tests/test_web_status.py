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
