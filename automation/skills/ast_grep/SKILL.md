---
name: ast_grep
description: Structural syntax tree search and replacement using ast-grep (sg) CLI across C++, Go, TypeScript, Python, and GDScript instead of broad text dumps.
---

# Structural Search & Rewrite (`ast_grep`)

## Mandatory Rule: Prefer Structural AST Search Over Broad Grep Dumps
Use `ast-grep` (`sg`) to match syntax tree patterns and perform surgical structural rewrites instead of dumping large text files or running unbounded regex searches.

## Usage Patterns
1. **Structural Search (Concise Match Output):**
   ```bash
   ast-grep run -p '$OBJ.$METHOD($$$ARGS)' -l ts src/
   ast-grep run -p 'if ($ERR != nil) { $$$BODY }' -l go .
   ast-grep run -p 'def $FUNC($$$ARGS) -> $RET:' -l py automation/
   ```
2. **In-Place Structural Rewrite (Dry-Run Diff First):**
   ```bash
   ast-grep run -p '$OLD_CALL($$$ARGS)' -r '$NEW_CALL($$$ARGS)' -l cpp src/
   # Apply in place with -U:
   ast-grep run -p '$OLD_CALL($$$ARGS)' -r '$NEW_CALL($$$ARGS)' -l cpp -U src/
   ```
3. **Supported Languages (`-l`):**
   - `cpp`, `c`, `go`, `ts`, `tsx`, `js`, `py`
