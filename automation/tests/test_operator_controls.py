from __future__ import annotations

from pathlib import Path

from openhands_factory.host_resource_gate import instance_priority
from openhands_factory.operator_controls import read_controls, write_controls


def test_malformed_operator_file_means_no_override(tmp_path: Path) -> None:
    path = tmp_path / "elgansayer__neptune.json"
    path.write_text("{not json")
    assert read_controls("elgansayer/Neptune", tmp_path) == {}


def test_operator_priority_overrides_instance_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FACTORY_OPERATOR_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "elgansayer/workout-agent")
    monkeypatch.setenv("FACTORY_INSTANCE_PRIORITY", "normal")
    assert instance_priority() == "normal"
    write_controls("elgansayer/workout-agent", {"priority": "high"}, tmp_path)
    assert instance_priority() == "high"
