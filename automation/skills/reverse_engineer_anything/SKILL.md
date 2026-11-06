---
name: reverse_engineer_anything
description: Token-efficient binary reverse engineering via headless GhidraMCP (pyghidra bridge) and REA (reverse_engineer_anything). Enforces targeted single-function queries without dumping full strings or whole assembly files.
---

# Reverse Engineer Anything (`REA` & Headless `GhidraMCP`)

## Mandatory Rule: Targeted Binary Queries Only
When reverse engineering ELF, PE, COFF, or Saturn/Godot native binaries:
- **NEVER** dump raw hex (`xxd`, `hexdump`) or unfiltered `objdump -d` / `strings` outputs into prompt context.
- **ALWAYS** query symbol tables first and decompile **one candidate function at a time** via `GhidraMCP` or `reverse_engineer_anything`.

## Available MCP Tools (and CLI Equivalents)
1. **`list_imports_exports(binary_path, filter_pattern="", kind="all", limit=50)`**
   - Lists imported/exported symbols and addresses with bounded pagination.
   - CLI: `ghidra-mcp --cli list_imports_exports --binary <path> --filter <pattern>`
2. **`query_symbol_address(binary_path, symbol_or_address)`**
   - Resolves exact symbol addresses, sizes, and active in-place rename mappings.
   - CLI: `ghidra-mcp --cli query_symbol_address --binary <path> --symbol <name_or_hex>`
3. **`decompile_function(binary_path, target_function, max_lines=80)`**
   - Decompiles/lifts a **single** target function via headless `pyghidra` or bounded single-symbol disassembly with in-place symbol renames applied.
   - CLI: `ghidra-mcp --cli decompile_function --binary <path> --symbol <func_name>`
4. **`rename_symbol(binary_path, old_name_or_address, new_name)`**
   - Records an in-place symbol rename in `<binary>.ghidra_symbols.json` so subsequent queries and single-function decompilations reflect semantic names without touching binary bytes.
   - CLI: `ghidra-mcp --cli rename_symbol --binary <path> --symbol <old> --new-name <new>`

Note: via Code Mode call `factory_codemode.execute(server="GhidraMCP", tool=..., args=JSON)`; lists return TOON, 15 per page, `has_more`.
