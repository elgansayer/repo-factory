---
name: serena_slim
description: Symbolic LSP editing via the slim Serena MCP. Prefer replace_symbol_body / find_referencing_symbols over line-based diffs.
---
# Serena Slim (`serena_slim`)
Registered alongside `treesitter_mcp`. Use treesitter_mcp to orient (signatures), then Serena to act:
- `find_symbol` / `find_referencing_symbols`: locate a symbol and its callers, no file reads.
- `replace_symbol_body`: replace one function/method/class body. Preferred over line-based edits.
- `insert_after_symbol` / `insert_before_symbol`: add code at symbol boundaries.
Rules: never read a whole file to edit one symbol; never emit line-number diffs when a symbolic edit works.
