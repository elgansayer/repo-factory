"""The Elgl-owned contract list must name real tests, so skips cannot silently rot."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_listed_elgl_contract_names_an_existing_test() -> None:
    listed = [
        line.strip()
        for line in (ROOT / "tests" / "elgl_repository_contracts.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert listed and len(listed) == len(set(listed))
    for node in listed:
        path, name = node.split("::")
        source = (ROOT / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE), node
