#!/opt/hellotalk-factory/venv/bin/python
"""REA (`reverse_engineer_anything`) Token-Efficient Binary Analysis MCP Server."""

import os
import runpy
from pathlib import Path

os.environ["RE_MCP_SERVER_NAME"] = "reverse_engineer_anything"
_GHIDRA_BRIDGE = Path(__file__).with_name("ghidra_mcp.py")
if __name__ == "__main__":
    runpy.run_path(str(_GHIDRA_BRIDGE), run_name="__main__")
else:
    globals().update(runpy.run_path(str(_GHIDRA_BRIDGE)))
