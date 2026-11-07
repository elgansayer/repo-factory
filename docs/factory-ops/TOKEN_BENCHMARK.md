# Token benchmark

Local fixture benchmark; cl100k_base tokenizer. No remote model requests or billed-token claims.

| Measurement | Before tokens | After tokens | Reduction |
|---|---:|---:|---:|
| RE symbol table: same 15 rows, JSON vs TOON | 1050 | 495 | 52.86% |
| RE task: full disassembly vs selected function | 6636 | 23 | 99.65% |
| C++ failed compilation: raw vs RTK/error filter | 2220 | 1358 | 38.83% |
| Caveman local engine: same C++ diagnostic input | 2220 | 2220 | 0.0% |
| MCP definitions: four direct servers vs Code Mode | 4260 | 244 | 94.27% |

Current generic system prompt: 712 tokens. Code Mode definitions: 244 tokens. Combined: 956 tokens.

Redis: identical opted-in request hit (one backend call for two requests); changed input missed. This saves 50% of backend requests in a two-request repetition fixture, not 50% of general agent traffic.

RE full-disassembly comparison measures a targeted workflow change, not lossless compression. The same-row TOON measurement is lossless for this fixture. Compilation exit status and actual error lines were preserved.

Caveman engine, proxy and recovery MCP are installed with verified download checksums. Local engine compression is measured above; the existing 712-token system contract alone measured 0% reduction. No live model A/B trial was run, so no model-output saving or provider billing discount is claimed. Serena performed an actual symbolic edit. Ghidra decompiled and renamed an actual compiled function.

All five parsers, gateway pagination, and four backend tool catalogs passed.
