---
name: ponytail_guardrails
description: Minimal-viable-implementation guardrails. Zero speculative abstractions, no unrequested refactors. Always active for factory workers.
---
# Ponytail Guardrails
1. Implement exactly what the task states. Smallest diff that satisfies the acceptance criteria and tests.
2. No speculative abstractions: no new interfaces, base classes, config flags, plugin hooks, or helpers with a single caller.
3. No unrequested refactors, renames, reformatting, dependency bumps, or file moves. Touch only lines the change requires.
4. Reuse existing code and patterns before writing new code. Prefer deleting over adding.
5. No new tests/docs beyond what the change requires; do not rewrite passing tests.
6. If scope seems larger than the task, stop and report the gap in one line instead of expanding.
