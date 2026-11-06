#!/opt/hellotalk-factory/venv/bin/python
"""Treesitter AST MCP Server (`treesitter_mcp`) for C++, Go, TypeScript, Python, and GDScript.

Exposes token-saving structural code inspection tools:
- `code_map`: Compact AST map of a file or directory (classes, structs, functions, line ranges).
- `type_map`: Type definitions (classes, structs, interfaces, enums, type aliases, GDScript classes/signals).
- `view_code`: View file in `signatures` mode (default) or bounded line/symbol range (max 150 lines).
- `minimal_edit_context`: Fetch only the target symbol's AST node + surrounding context lines.
- `call_graph`: Extract caller -> callee relationships from AST call expressions.
- `find_usages`: Find structural identifier usages across supported languages without full-text dumps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tree_sitter
import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_python
import tree_sitter_typescript
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_common import respond, spill  # noqa: E402

mcp = FastMCP("treesitter_mcp")

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "build",
        "bin",
        "dist",
        "out",
        ".import",
        ".godot",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        "coverage",
        "factory-ghidra-projects",
        ".mcp-out",
        ".serena",
    }
)

EXT_TO_LANG: dict[str, str] = {
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "typescript",
    ".jsx": "tsx",
    ".py": "python",
    ".gd": "gdscript",
}

_PARSERS: dict[str, tree_sitter.Parser] = {}


def _get_parser(lang: str) -> tree_sitter.Parser | None:
    if lang in _PARSERS:
        return _PARSERS[lang]
    if lang == "cpp":
        language = tree_sitter.Language(tree_sitter_cpp.language())
    elif lang == "go":
        language = tree_sitter.Language(tree_sitter_go.language())
    elif lang == "typescript":
        language = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    elif lang == "tsx":
        language = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    elif lang == "python":
        language = tree_sitter.Language(tree_sitter_python.language())
    elif lang == "gdscript":
        import tree_sitter_gdscript
        language = tree_sitter.Language(tree_sitter_gdscript.language())
    else:
        return None
    parser = tree_sitter.Parser(language)
    _PARSERS[lang] = parser
    return parser


@dataclass
class SymbolEntry:
    name: str
    kind: str  # "function", "method", "class", "struct", "interface", "enum", "type", "signal"
    signature: str
    start_line: int
    end_line: int
    parent: str | None = None
    calls: list[str] = field(default_factory=list)


def _iter_source_files(root: Path, max_depth: int = 4, max_files: int = 80) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in EXT_TO_LANG else []
    results: list[Path] = []
    root = root.resolve()

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth or len(results) >= max_files:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_file(), p.name))
        except OSError:
            return
        for entry in entries:
            if len(results) >= max_files:
                return
            if entry.name.startswith(".") and entry.name not in {".github"}:
                continue
            if entry.is_dir():
                if entry.name in IGNORED_DIRS:
                    continue
                _walk(entry, depth + 1)
            elif entry.is_file() and entry.suffix.lower() in EXT_TO_LANG:
                results.append(entry)

    _walk(root, 0)
    return results


def _parse_gdscript(source: str) -> list[SymbolEntry]:
    """Structured AST-equivalent parser for Godot GDScript (.gd) files."""
    lines = source.splitlines()
    entries: list[SymbolEntry] = []
    n = len(lines)

    def _find_block_end(start_idx: int, base_indent: int) -> int:
        end_idx = start_idx
        for i in range(start_idx + 1, n):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip(" \t"))
            if indent <= base_indent and not stripped.startswith((")", "]", "}")):
                break
            end_idx = i
        return end_idx + 1

    current_class: str | None = None
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))
        if indent == 0:
            current_class = None

        if m := re.match(r"^class_name\s+([A-Za-z_]\w*)", stripped):
            entries.append(
                SymbolEntry(
                    name=m.group(1),
                    kind="class",
                    signature=stripped,
                    start_line=idx + 1,
                    end_line=n,
                )
            )
        elif m := re.match(r"^class\s+([A-Za-z_]\w*)", stripped):
            end_line = _find_block_end(idx, indent)
            current_class = m.group(1)
            entries.append(
                SymbolEntry(
                    name=current_class,
                    kind="class",
                    signature=stripped.rstrip(":"),
                    start_line=idx + 1,
                    end_line=end_line,
                )
            )
        elif m := re.match(r"^signal\s+([A-Za-z_]\w*)", stripped):
            entries.append(
                SymbolEntry(
                    name=m.group(1),
                    kind="signal",
                    signature=stripped,
                    start_line=idx + 1,
                    end_line=idx + 1,
                    parent=current_class,
                )
            )
        elif m := re.match(r"^enum\s+([A-Za-z_]\w*)?", stripped):
            end_line = idx + 1
            if "{" in stripped and "}" not in stripped:
                for j in range(idx + 1, min(idx + 50, n)):
                    if "}" in lines[j]:
                        end_line = j + 1
                        break
            entries.append(
                SymbolEntry(
                    name=m.group(1) or "<anonymous_enum>",
                    kind="enum",
                    signature=stripped.split("{")[0].strip(),
                    start_line=idx + 1,
                    end_line=end_line,
                    parent=current_class,
                )
            )
        elif m := re.match(r"^(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?", stripped):
            end_line = _find_block_end(idx, indent)
            body_text = "\n".join(lines[idx + 1 : end_line])
            calls = sorted(
                {
                    c
                    for c in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body_text)
                    if c not in {"if", "elif", "while", "for", "match", "return", "print", "super"}
                }
            )
            entries.append(
                SymbolEntry(
                    name=m.group(1),
                    kind="method" if current_class else "function",
                    signature=stripped.rstrip(":").strip(),
                    start_line=idx + 1,
                    end_line=end_line,
                    parent=current_class,
                    calls=calls[:25],
                )
            )
    return entries


def _first_line_signature(node: tree_sitter.Node, source_bytes: bytes) -> str:
    text = source_bytes[node.start_byte : min(node.end_byte, node.start_byte + 400)].decode(
        "utf-8", errors="replace"
    )
    first_line = text.splitlines()[0].strip() if text else ""
    for stopper in ("{", ":"):
        if first_line.endswith(stopper):
            first_line = first_line[:-1].strip()
    return first_line[:180]


def _extract_calls_from_node(node: tree_sitter.Node, source_bytes: bytes) -> list[str]:
    calls: set[str] = set()
    stack = [node]
    while stack:
        curr = stack.pop()
        if curr.type in {"call", "call_expression"}:
            func_node = curr.child_by_field_name("function")
            if func_node is not None:
                raw = source_bytes[func_node.start_byte : func_node.end_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
                if raw and len(raw) <= 64:
                    calls.add(raw.split(".")[-1].split("::")[-1])
        stack.extend(curr.children)
    return sorted(calls)[:25]


def _extract_name(node: tree_sitter.Node, source_bytes: bytes) -> str:
    name_node = node.child_by_field_name("name") or node.child_by_field_name("declarator")
    if name_node is not None:
        while name_node.type in {
            "function_declarator",
            "pointer_declarator",
            "reference_declarator",
            "parenthesized_declarator",
        }:
            inner = name_node.child_by_field_name("declarator") or (
                name_node.NamedChildren[0] if hasattr(name_node, "NamedChildren") else None
            )
            if inner is None:
                for ch in name_node.children:
                    if ch.is_named:
                        inner = ch
                        break
            if inner is None:
                break
            name_node = inner
        return source_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", errors="replace"
        ).strip()
    for ch in node.children:
        if ch.type in {"identifier", "type_identifier", "field_identifier", "property_identifier"}:
            return source_bytes[ch.start_byte : ch.end_byte].decode("utf-8", errors="replace").strip()
    return "<anonymous>"


def parse_file_symbols(file_path: Path) -> list[SymbolEntry]:
    ext = file_path.suffix.lower()
    lang = EXT_TO_LANG.get(ext)
    if not lang:
        return []
    try:
        source_bytes = file_path.read_bytes()
    except OSError:
        return []
    parser = _get_parser(lang)
    if parser is None:
        return []
    tree = parser.parse(source_bytes)
    entries: list[SymbolEntry] = []

    type_nodes = {
        "class_definition": "class",
        "class_declaration": "class",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "interface_declaration": "interface",
        "enum_specifier": "enum",
        "enum_declaration": "enum",
        "type_alias_declaration": "type",
        "type_declaration": "type",
        "signal_statement": "signal",
    }
    func_nodes = {
        "function_definition": "function",
        "function_declaration": "function",
        "method_definition": "method",
        "method_declaration": "method",
    }

    def _visit(node: tree_sitter.Node, parent_name: str | None = None) -> None:
        ntype = node.type
        current_parent = parent_name
        if ntype in type_nodes:
            name = _extract_name(node, source_bytes)
            if name and name != "<anonymous>":
                entries.append(
                    SymbolEntry(
                        name=name,
                        kind=type_nodes[ntype],
                        signature=_first_line_signature(node, source_bytes),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent=parent_name,
                    )
                )
                current_parent = name
        elif ntype in func_nodes:
            name = _extract_name(node, source_bytes)
            entries.append(
                SymbolEntry(
                    name=name,
                    kind="method" if parent_name else func_nodes[ntype],
                    signature=_first_line_signature(node, source_bytes),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent_name,
                    calls=_extract_calls_from_node(node, source_bytes),
                )
            )
            return  # Avoid descending into nested local functions unless desired

        for child in node.children:
            if child.is_named:
                _visit(child, current_parent)

    _visit(tree.root_node)
    return entries


def _rel(fpath: Path, base: Path) -> str:
    return str(fpath.relative_to(base)) if fpath.is_relative_to(base) else str(fpath)


@mcp.tool()
def code_map(path: str, max_depth: int = 3, page: int = 1, fields: str = "") -> str:
    """AST code map rows (file,kind,name,sig,lines). 15 per page with has_more. `fields` projects columns."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"Error: Path not found: {path}"
    base = target if target.is_dir() else target.parent
    rows = [
        {"file": _rel(f, base), "kind": s.kind, "name": s.name, "parent": s.parent or "",
         "sig": s.signature, "lines": f"{s.start_line}-{s.end_line}"}
        for f in _iter_source_files(target, max_depth=max_depth)
        for s in parse_file_symbols(f)
    ]
    return respond("symbols", rows, default_fields=["file", "kind", "name", "sig", "lines"], fields=fields, page=page)


@mcp.tool()
def type_map(path: str, page: int = 1, fields: str = "") -> str:
    """Type definitions (class/struct/interface/enum/type/signal) rows, 15 per page with has_more."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"Error: Path not found: {path}"
    base = target if target.is_dir() else target.parent
    kinds = {"class", "struct", "interface", "enum", "type", "signal"}
    rows = [
        {"file": _rel(f, base), "kind": s.kind, "name": s.name, "sig": s.signature, "lines": f"{s.start_line}-{s.end_line}"}
        for f in _iter_source_files(target, max_depth=4)
        for s in parse_file_symbols(f) if s.kind in kinds
    ]
    return respond("types", rows, default_fields=["file", "kind", "name", "lines"], fields=fields, page=page)


def _view_code_raw(
    file_path: str,
    mode: str = "signatures",
    start_line: int = 0,
    end_line: int = 0,
    symbol: str = "",
) -> str:
    """Inspect a source file without full-file dumps.

    - `mode="signatures"` (default) or `mode="ast_summary"`: returns only imports, types, and function signatures with line numbers.
    - `symbol="<name>"`: returns only the implementation of `<name>` (capped at 150 lines).
    - `start_line` / `end_line`: returns the bounded line range (strictly capped at 150 lines).
    """
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        return f"Error: File not found: {file_path}"
    try:
        lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Error reading {file_path}: {exc}"

    total_lines = len(lines)
    symbols = parse_file_symbols(fpath)

    if symbol:
        for sym in symbols:
            if sym.name == symbol or sym.name.endswith(f"::{symbol}") or sym.name.endswith(f".{symbol}"):
                s_line = max(1, sym.start_line)
                e_line = min(total_lines, min(sym.end_line, s_line + 149))
                snippet = [f"{ln:4d} | {lines[ln - 1]}" for ln in range(s_line, e_line + 1)]
                trunc = " (truncated at 150-line cap)" if sym.end_line > e_line else ""
                return (
                    f"# {fpath.name} :: {sym.name} (L{s_line}-L{e_line} of {total_lines}{trunc})\n"
                    + "\n".join(snippet)
                )
        return f"Symbol {symbol!r} not found in {fpath.name}. Call view_code(mode='signatures') first."

    if start_line > 0 and end_line >= start_line:
        s_line = max(1, start_line)
        e_line = min(total_lines, min(end_line, s_line + 149))
        snippet = [f"{ln:4d} | {lines[ln - 1]}" for ln in range(s_line, e_line + 1)]
        return f"# {fpath.name} (L{s_line}-L{e_line} of {total_lines})\n" + "\n".join(snippet)

    # Default: signatures / ast_summary mode
    imports = [
        f"L{idx + 1}: {line.strip()}"
        for idx, line in enumerate(lines[:80])
        if line.strip().startswith(
            ("import ", "from ", "#include", "package ", "using ", "extends ", "class_name ")
        )
    ]
    out = [f"# Signatures & AST Summary: {fpath.name} ({total_lines} lines total)"]
    if imports:
        out.append("## Imports / Headers")
        out.extend(f"  {imp}" for imp in imports[:25])
    out.append("## Symbols")
    if symbols:
        for sym in symbols:
            parent_tag = f" ({sym.parent})" if sym.parent else ""
            out.append(
                f"  - L{sym.start_line}-L{sym.end_line} [{sym.kind}{parent_tag}] `{sym.signature}`"
            )
    else:
        # Fallback for non-AST files <= 150 lines
        preview_end = min(total_lines, 60)
        out.append(f"  (No AST symbols extracted; showing first {preview_end} lines)")
        out.extend(f"  {i + 1:4d} | {lines[i]}" for i in range(preview_end))
    return "\n".join(out)


@mcp.tool()
def view_code(file_path: str, mode: str = "signatures", start_line: int = 0, end_line: int = 0, symbol: str = "") -> str:
    """Signatures (default), symbol body, or <=150 line range. Oversized output spills to a temp file."""
    return spill(_view_code_raw(file_path, mode, start_line, end_line, symbol), "view_code", max_lines=160, max_chars=9000)


@mcp.tool()
def minimal_edit_context(file_path: str, symbol: str, context_lines: int = 3) -> str:
    """Return ONLY the target symbol's AST node lines plus `context_lines` of surrounding context for minimal-token edits."""
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        return f"Error: File not found: {file_path}"
    lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    symbols = parse_file_symbols(fpath)
    matches = [
        s
        for s in symbols
        if s.name == symbol or s.name.endswith(f"::{symbol}") or s.name.endswith(f".{symbol}")
    ]
    if not matches:
        return f"Error: Symbol {symbol!r} not found in {fpath.name}. Available symbols: {', '.join(s.name for s in symbols[:30])}"
    sym = matches[0]
    s_line = max(1, sym.start_line - max(0, context_lines))
    e_line = min(total, min(sym.end_line + max(0, context_lines), s_line + 149))
    body = "\n".join(f"{ln:4d} | {lines[ln - 1]}" for ln in range(s_line, e_line + 1))
    return (
        f"# Minimal Edit Context: {fpath.name} :: {sym.name} "
        f"(Symbol L{sym.start_line}-L{sym.end_line}, Context L{s_line}-L{e_line})\n{body}"
    )


@mcp.tool()
def call_graph(path: str, symbol: str = "", page: int = 1, fields: str = "") -> str:
    """Caller -> callees rows, 15 per page with has_more."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"Error: Path not found: {path}"
    base = target if target.is_dir() else target.parent
    rows = []
    for f in _iter_source_files(target, max_depth=3):
        for s in parse_file_symbols(f):
            if s.kind not in {"function", "method"}:
                continue
            if symbol and symbol not in s.name and symbol not in s.calls:
                continue
            rows.append({"file": _rel(f, base), "line": s.start_line,
                         "caller": f"{s.parent}.{s.name}" if s.parent else s.name,
                         "callees": ";".join(s.calls)})
    return respond("calls", rows, default_fields=["file", "line", "caller", "callees"], fields=fields, page=page)


@mcp.tool()
def find_usages(path: str, symbol: str, page: int = 1, fields: str = "") -> str:
    """Identifier usages rows (file,line,text), 15 per page with has_more."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return f"Error: Path not found: {path}"
    base = target if target.is_dir() else target.parent
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    rows = []
    for f in _iter_source_files(target, max_depth=4, max_files=120):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            if pattern.search(line):
                rows.append({"file": _rel(f, base), "line": idx + 1, "text": line.strip()[:120]})
                if len(rows) >= 300:
                    break
    return respond("usages", rows, default_fields=["file", "line", "text"], fields=fields, page=page)


def main() -> None:
    parser = argparse.ArgumentParser(description="Treesitter AST MCP Server")
    parser.add_argument("--cli", choices=["code_map", "type_map", "view_code", "minimal_edit_context", "call_graph", "find_usages"], help="Run tool directly via CLI")
    parser.add_argument("--path", default=".", help="Target file or directory path")
    parser.add_argument("--symbol", default="", help="Symbol name")
    parser.add_argument("--mode", default="signatures", help="view_code mode")
    parser.add_argument("--start-line", type=int, default=0)
    parser.add_argument("--end-line", type=int, default=0)
    args, _ = parser.parse_known_args()

    if args.cli:
        if args.cli == "code_map":
            print(code_map(args.path))
        elif args.cli == "type_map":
            print(type_map(args.path))
        elif args.cli == "view_code":
            print(view_code(args.path, mode=args.mode, start_line=args.start_line, end_line=args.end_line, symbol=args.symbol))
        elif args.cli == "minimal_edit_context":
            print(minimal_edit_context(args.path, symbol=args.symbol))
        elif args.cli == "call_graph":
            print(call_graph(args.path, symbol=args.symbol))
        elif args.cli == "find_usages":
            print(find_usages(args.path, symbol=args.symbol))
        return

    mcp.run()


if __name__ == "__main__":
    main()
