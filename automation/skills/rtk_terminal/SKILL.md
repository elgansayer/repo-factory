---
name: rtk_terminal
description: Run shell/compiler commands through rtk so output is deduplicated, progress bars stripped, stack traces cut to root failure.
---
# RTK terminal output compression
Prefix noisy commands with `rtk` (`rtk err <cmd>`, `rtk test <cmd>`, `rtk git diff`, `rtk grep`, `rtk read <file>`, `rtk log <file>`).
Factory PATH shims already route cmake/make/go/godot through `concise-build`, which pipes through `rtk err`.
Set FACTORY_RAW_BUILD_LOGS=1 only when the compressed output hides the root cause.
