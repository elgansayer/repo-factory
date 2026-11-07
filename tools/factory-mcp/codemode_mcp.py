#!/opt/hellotalk-factory/venv/bin/python
"""`factory_codemode`: MCP Code Mode gateway.

Wraps treesitter_mcp, GhidraMCP, reverse_engineer_anything and serena_slim behind exactly two
tools so the per-turn tool-definition cost stays tiny:

  schema(server="", tool="")  -> compact catalog, or one tool's full JSON schema on demand
  execute(server, tool, args, fields="", page=1) -> shaped result

Result shaping (server side, before anything reaches the agent context):
  * JSON list results are field-projected, paged at 15 with `has_more`, serialised as TOON
  * oversized free text is written to a workspace temp file; only a 3 line summary + path returned
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mcp_common import respond, spill  # noqa: E402

PY = "/opt/hellotalk-factory/venv/bin/python"
WORKSPACE = os.environ.get("FACTORY_WORKSPACE") or os.getcwd()

SERVERS: dict[str, StdioServerParameters] = {
    "treesitter_mcp": StdioServerParameters(command=PY, args=[str(HERE / "treesitter_mcp.py")]),
    "GhidraMCP": StdioServerParameters(command=PY, args=[str(HERE / "ghidra_mcp.py")]),
    "reverse_engineer_anything": StdioServerParameters(command=PY, args=[str(HERE / "rea_mcp.py")]),
    "serena_slim": StdioServerParameters(
        command=os.environ.get("SERENA_SLIM_BIN", "/home/dev/.local/bin/serena-slim"), args=[WORKSPACE]
    ),
}

mcp = FastMCP("factory_codemode")


class _Backend:
    """One owner task per backend: stdio/anyio scopes must be entered and exited by the same task."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.queue: asyncio.Queue[tuple[str, tuple, asyncio.Future]] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future | None = None

    async def _run(self) -> None:
        try:
            async with stdio_client(SERVERS[self.name]) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert self.ready is not None
                    self.ready.set_result(True)
                    while True:
                        method, args, fut = await self.queue.get()
                        try:
                            value = await getattr(session, method)(*args)
                            if not fut.done():
                                fut.set_result(value)
                        except Exception as exc:
                            if not fut.done():
                                fut.set_exception(exc)
        except BaseException as exc:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError("backend stopped"))

    async def call(self, method: str, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        if self.task is None or self.task.done():
            self.ready = loop.create_future()
            self.task = loop.create_task(self._run())
        await asyncio.wait_for(asyncio.shield(self.ready), timeout=120)
        fut: asyncio.Future = loop.create_future()
        await self.queue.put((method, args, fut))
        return await asyncio.wait_for(fut, timeout=300)


_backends: dict[str, _Backend] = {}


class _Handle:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    async def list_tools(self) -> Any:
        return await self.backend.call("list_tools")

    async def call_tool(self, name: str, args: dict) -> Any:
        return await self.backend.call("call_tool", name, args)


async def _session(server: str) -> _Handle:
    if server not in SERVERS:
        raise ValueError(f"unknown server {server!r}; one of {sorted(SERVERS)}")
    return _Handle(_backends.setdefault(server, _Backend(server)))


def _sig(tool: Any) -> str:
    props = (tool.inputSchema or {}).get("properties", {})
    required = set((tool.inputSchema or {}).get("required", []))
    args = ",".join(f"{k}{'' if k in required else '?'}" for k in props)
    doc = (tool.description or "").strip().split("\n")[0][:70]
    return f"{tool.name}({args}) {doc}"


@mcp.tool()
async def schema(server: str = "", tool: str = "") -> str:
    """Catalog of backend tools. No args: one line per tool. With server+tool: full JSON schema."""
    if server and tool:
        listed = (await (await _session(server)).list_tools()).tools
        for t in listed:
            if t.name == tool:
                return json.dumps({"name": t.name, "description": t.description, "input": t.inputSchema}, separators=(",", ":"))
        return f"unknown tool {tool!r} on {server}"
    names = [server] if server else [s for s in SERVERS if s != "serena_slim" or os.environ.get("FACTORY_CODEMODE_SERENA", "1") == "1"]
    out = []
    for name in names:
        try:
            listed = (await (await _session(name)).list_tools()).tools
        except Exception as exc:  # backend unavailable: report, keep the rest usable
            out.append(f"{name}: unavailable ({type(exc).__name__})")
            continue
        out.append(f"{name}:")
        out.extend(f"  {_sig(t)}" for t in listed)
    return "\n".join(out)


def _shape(text: str, label: str, fields: str, page: int) -> str:
    stripped = text.lstrip()
    if stripped.startswith("page=") or stripped.startswith("[spilled]"):
        return text  # already shaped by the backend
    try:
        data = json.loads(text)
    except ValueError:
        return spill(text, label)
    rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), None) if isinstance(data, dict) else None
    if rows and all(isinstance(r, dict) for r in rows):
        return respond(label, rows, default_fields=list(rows[0].keys())[:6], fields=fields, page=page)
    return spill(json.dumps(data, separators=(",", ":")), label)


@mcp.tool()
async def execute(server: str, tool: str, args: str = "{}", fields: str = "", page: int = 1) -> str:
    """Run backend tool. args = JSON object string. fields = comma list to project. page starts at 1 (15 items/page)."""
    try:
        payload = json.loads(args) if isinstance(args, str) else dict(args)
        workspace = Path(WORKSPACE).resolve()
        for key in ("path", "file_path", "binary_path", "relative_path"):
            if payload.get(key):
                candidate = Path(payload[key]).expanduser()
                candidate = (workspace / candidate).resolve()
                if not candidate.is_relative_to(workspace):
                    raise ValueError("Path outside assigned workspace")
        if server in {"treesitter_mcp", "GhidraMCP", "reverse_engineer_anything"}:
            listed = (await (await _session(server)).list_tools()).tools
            spec = next((t for t in listed if t.name == tool), None)
            props = (spec.inputSchema or {}).get("properties", {}) if spec else {}
            if "page" in props:
                payload["page"] = max(1, page)
            if "fields" in props and fields:
                payload["fields"] = fields
        session = await _session(server)
        result = await session.call_tool(tool, payload)
    except Exception as exc:
        return f"error: {type(exc).__name__}: {str(exc)[:300]}"
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.isError:
        return f"error: {text[:400]}"
    return _shape(text, f"{server}.{tool}", fields, page)


if __name__ == "__main__":
    mcp.run()
