#!/opt/hellotalk-factory/venv/bin/python
"""Headless GhidraMCP (`pyghidra` bridge) & Bounded Binary Symbol MCP Server.

Enforces Phase 3.2 & Phase 4.2 token-efficiency constraints:
- Lists imports/exports with bounded pagination (`list_imports_exports`).
- Queries specific symbol addresses (`query_symbol_address`).
- Decompiles a SINGLE target function at a time (`decompile_function`) via headless `pyghidra`
  or bounded single-symbol disassembly/CFG lifting.
- Renames symbols in place (`rename_symbol`) via persistent `.ghidra_symbols.json` / pyghidra state.
- Strictly blocks raw hex dumps, unfiltered `strings`, or whole-binary assembly dumps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
import sys
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_common import respond, spill  # noqa: E402
import pefile

SERVER_NAME = os.environ.get("RE_MCP_SERVER_NAME", "GhidraMCP")
mcp = FastMCP(SERVER_NAME)


def _symbol_map_path(binary_path: Path) -> Path:
    return binary_path.with_suffix(binary_path.suffix + ".ghidra_symbols.json")


def _load_renames(binary_path: Path) -> dict[str, str]:
    spath = _symbol_map_path(binary_path)
    if spath.is_file():
        try:
            data = json.loads(spath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def _save_renames(binary_path: Path, renames: dict[str, str]) -> None:
    spath = _symbol_map_path(binary_path)
    spath.write_text(json.dumps(renames, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_elf_symbols(binary_path: Path) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    renames = _load_renames(binary_path)
    with binary_path.open("rb") as f:
        elf = ELFFile(f)
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for sym in section.iter_symbols():
                if not sym.name:
                    continue
                addr = int(sym.entry["st_value"])
                size = int(sym.entry["st_size"])
                stype = sym.entry["st_info"]["type"]
                bind = sym.entry["st_info"]["bind"]
                shndx = sym.entry["st_shndx"]
                direction = "import" if shndx == "SHN_UNDEF" else "export"
                addr_hex = f"0x{addr:08x}"
                effective_name = renames.get(sym.name, renames.get(addr_hex, sym.name))
                symbols.append(
                    {
                        "name": effective_name,
                        "original_name": sym.name,
                        "address": addr_hex,
                        "size": size,
                        "type": stype,
                        "bind": bind,
                        "kind": direction,
                    }
                )
    return symbols


def _extract_pe_symbols(binary_path: Path) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    renames = _load_renames(binary_path)
    pe = pefile.PE(str(binary_path), fast_load=True)
    pe.parse_data_directories(
        directories=[
            pefile.DIRECTORY_entry["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_entry["IMAGE_DIRECTORY_ENTRY_EXPORT"],
        ]
    )
    image_base = int(pe.OPTIONAL_HEADER.ImageBase)
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll_name = entry.dll.decode("utf-8", errors="replace") if entry.dll else "unknown.dll"
            for imp in entry.imports:
                raw_name = (
                    imp.name.decode("utf-8", errors="replace")
                    if imp.name
                    else f"ordinal_{imp.ordinal}"
                )
                addr_hex = f"0x{int(imp.address):08x}" if imp.address else "0x00000000"
                effective = renames.get(raw_name, renames.get(addr_hex, raw_name))
                symbols.append(
                    {
                        "name": effective,
                        "original_name": raw_name,
                        "address": addr_hex,
                        "size": 0,
                        "type": f"IMPORT:{dll_name}",
                        "bind": "GLOBAL",
                        "kind": "import",
                    }
                )
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            raw_name = (
                exp.name.decode("utf-8", errors="replace")
                if exp.name
                else f"ordinal_{exp.ordinal}"
            )
            addr_hex = f"0x{image_base + int(exp.address):08x}"
            effective = renames.get(raw_name, renames.get(addr_hex, raw_name))
            symbols.append(
                {
                    "name": effective,
                    "original_name": raw_name,
                    "address": addr_hex,
                    "size": 0,
                    "type": "EXPORT",
                    "bind": "GLOBAL",
                    "kind": "export",
                }
            )
    return symbols


def _load_symbols(binary_path: Path) -> list[dict[str, Any]]:
    magic = binary_path.read_bytes()[:4]
    if magic == b"\x7fELF":
        return _extract_elf_symbols(binary_path)
    if magic[:2] == b"MZ":
        return _extract_pe_symbols(binary_path)
    # Fallback via nm -D / nm for archive or Mach-O/COFF objects
    proc = subprocess.run(
        ["nm", "-C", "--defined-only", str(binary_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    renames = _load_renames(binary_path)
    results: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[:500]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            addr_hex = f"0x{parts[0]}"
            raw_name = parts[2].strip()
            results.append(
                {
                    "name": renames.get(raw_name, renames.get(addr_hex, raw_name)),
                    "original_name": raw_name,
                    "address": addr_hex,
                    "size": 0,
                    "type": parts[1],
                    "bind": "GLOBAL",
                    "kind": "export" if parts[1].isupper() else "local",
                }
            )
    return results


@mcp.tool()
def list_imports_exports(binary_path: str, filter_pattern: str = "", kind: str = "all", page: int = 1, fields: str = "") -> str:
    """Imports/exports rows, 15 per page with has_more. Never dumps raw strings."""
    bpath = Path(binary_path).expanduser().resolve()
    if not bpath.is_file():
        return f"Error: Binary not found: {binary_path}"
    try:
        symbols = _load_symbols(bpath)
    except Exception as exc:
        return f"Error parsing binary symbols for {bpath.name}: {exc}"
    if kind in {"import", "export"}:
        symbols = [s for s in symbols if s["kind"] == kind]
    if filter_pattern:
        pat = re.compile(re.escape(filter_pattern), re.IGNORECASE)
        symbols = [s for s in symbols if pat.search(s["name"]) or pat.search(s["original_name"])]
    return respond("symbols", symbols, default_fields=["kind", "address", "name", "size"], fields=fields, page=page)


@mcp.tool()
def query_symbol_address(binary_path: str, symbol_or_address: str, page: int = 1, fields: str = "") -> str:
    """Address/size/rename metadata rows for a symbol or hex address, 15 per page with has_more."""
    bpath = Path(binary_path).expanduser().resolve()
    if not bpath.is_file():
        return f"Error: Binary not found: {binary_path}"
    try:
        symbols = _load_symbols(bpath)
    except Exception as exc:
        return f"Error querying binary {bpath.name}: {exc}"
    needle = symbol_or_address.strip().lower()
    rows = [s for s in symbols if needle in s["name"].lower() or needle in s["original_name"].lower() or needle == s["address"].lower()]
    if not rows:
        return f"No symbol matching {symbol_or_address!r} in {bpath.name}."
    return respond("matches", rows, default_fields=["name", "original_name", "address", "size", "kind"], fields=fields, page=page)


def _decompile_raw(binary_path: str, target_function: str, max_lines: int = 80) -> str:
    """Real single-function decompilation; long results are spilled by the caller."""
    from ghidra_headless import run
    try:
        return run(Path(binary_path).expanduser().resolve(), target_function)
    except Exception as error:
        return f"Ghidra decompilation failed: {type(error).__name__}: {error}"


@mcp.tool()
def decompile_function(binary_path: str, target_function: str, max_lines: int = 80) -> str:
    """Decompile ONE function. Output beyond 40 lines spills to a temp file (3 line summary + path)."""
    return spill(_decompile_raw(binary_path, target_function, max_lines), "decompile")


@mcp.tool()
def rename_symbol(binary_path: str, old_name_or_address: str, new_name: str) -> str:
    """Rename a symbol or address in place without modifying the binary or dumping full tables."""
    bpath = Path(binary_path).expanduser().resolve()
    if not bpath.is_file():
        return f"Error: Binary not found: {binary_path}"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_::.]*", new_name.strip()):
        return f"Error: Invalid identifier name {new_name!r}"
    renames = _load_renames(bpath)
    renames[old_name_or_address.strip()] = new_name.strip()
    from ghidra_headless import run
    try:
        run(bpath, old_name_or_address.strip(), new_name.strip())
    except Exception as error:
        return f"Ghidra rename failed: {type(error).__name__}: {error}"
    _save_renames(bpath, renames)
    return (
        f"[{SERVER_NAME}] Renamed `{old_name_or_address.strip()}` -> `{new_name.strip()}` "
        f"in {_symbol_map_path(bpath).name} ({len(renames)} active symbol rename(s))."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{SERVER_NAME} Headless Binary Analysis MCP Server")
    parser.add_argument(
        "--cli",
        choices=["list_imports_exports", "query_symbol_address", "decompile_function", "rename_symbol"],
        help="Run tool directly via CLI",
    )
    parser.add_argument("--binary", default="", help="Path to target binary")
    parser.add_argument("--symbol", default="", help="Target symbol or address")
    parser.add_argument("--new-name", default="", help="New symbol name for rename_symbol")
    parser.add_argument("--filter", default="", help="Filter pattern")
    args, _ = parser.parse_known_args()

    if args.cli:
        if args.cli == "list_imports_exports":
            print(list_imports_exports(args.binary, filter_pattern=args.filter))
        elif args.cli == "query_symbol_address":
            print(query_symbol_address(args.binary, args.symbol))
        elif args.cli == "decompile_function":
            print(decompile_function(args.binary, args.symbol))
        elif args.cli == "rename_symbol":
            print(rename_symbol(args.binary, args.symbol, args.new_name))
        return

    mcp.run()


if __name__ == "__main__":
    main()
