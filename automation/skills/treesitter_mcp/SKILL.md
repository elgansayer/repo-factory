---
name: treesitter_mcp
description: AST-based structural code exploration and minimal-context editing for C++, Go, TypeScript, Python, and GDScript via treesitter_mcp. Use before reading or editing any file > 150 lines.
---

# Treesitter AST MCP Skill (`treesitter_mcp`)

## Mandatory Rule: No Full-File Reads (> 150 Lines)
Never run `cat` or read entire files exceeding 150 lines. Always inspect signatures first using `treesitter_mcp`, then fetch only the target function/method body.

## Available MCP Tools (and CLI Equivalents)
1. **`code_map(path, max_depth=3)`**
   - Returns a compact AST map of classes, structs, functions, and line ranges (`Lstart-Lend`).
   - CLI: `treesitter-mcp --cli code_map --path <dir_or_file>`
2. **`type_map(path)`**
   - Lists all classes, structs, interfaces, enums, GDScript signals, and type aliases.
   - CLI: `treesitter-mcp --cli type_map --path <dir_or_file>`
3. **`view_code(file_path, mode="signatures", start_line=0, end_line=0, symbol="")`**
   - Default `mode="signatures"` returns imports and function/class signatures without bodies.
   - Pass `symbol="<FuncName>"` or `start_line`/`end_line` (<= 150 lines) to inspect a single function.
   - CLI: `treesitter-mcp --cli view_code --path <file> --mode signatures`
4. **`minimal_edit_context(file_path, symbol, context_lines=3)`**
   - Fetches ONLY the target symbol's AST node lines plus `context_lines` of surrounding context for surgical edits.
   - CLI: `treesitter-mcp --cli minimal_edit_context --path <file> --symbol <FuncName>`
5. **`call_graph(path, symbol="")`**
   - Extracts static caller -> callee edges from AST call expressions.
   - CLI: `treesitter-mcp --cli call_graph --path <dir_or_file> --symbol <FuncName>`
6. **`find_usages(path, symbol, max_results=30)`**
   - Finds structural identifier usages across C++, Go, TypeScript, Python, and GDScript.
   - CLI: `treesitter-mcp --cli find_usages --path <dir_or_file> --symbol <Identifier>`

Note: via Code Mode call `factory_codemode.execute(server="treesitter_mcp", tool=..., args=JSON)`; lists return TOON, 15 per page, `has_more`.
