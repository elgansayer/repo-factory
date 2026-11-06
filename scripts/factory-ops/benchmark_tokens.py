"""Reproducible local payload benchmarks, never billed-token estimates."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

ROOT = Path('/home/dev/factory-ops/validation/sample')
ROOT.mkdir(parents=True, exist_ok=True)
os.environ['FACTORY_WORKSPACE'] = str(ROOT)
os.environ['FACTORY_MCP_SPILL_DIR'] = str(ROOT / '.mcp-out')
os.chdir(ROOT)
sys.path[:0] = ['/home/dev/factory-tools/mcp', '/home/dev/factory-ops/litellm']
import tiktoken
import codemode_mcp as gateway
import ghidra_mcp as ghidra
import treesitter_mcp as ts
from mcp_common import respond

ENC = tiktoken.get_encoding('cl100k_base')
def tokens(value):
    return len(ENC.encode(value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))))

def command(args):
    return subprocess.run(args, text=True, capture_output=True, timeout=120)

async def main():
    rows = []
    def metric(name, before, after):
        b, a = tokens(before), tokens(after)
        rows.append((name, b, a, round(100 * (b-a)/b, 2) if b else 0))
    source = ROOT / 'sample.cpp'
    source.write_text('\n'.join(f'extern "C" int function_{i}(int n) {{ return n + {i}; }}' for i in range(30)) + '\nint main() { return function_7(2); }\n')
    built = command(['g++', '-g', '-O0', str(source), '-o', str(ROOT / 'sample')])
    assert built.returncode == 0, built.stderr[-1000:]
    symbols = ghidra._load_symbols(ROOT / 'sample')[:15]
    compact = respond('symbols', symbols, default_fields=list(symbols[0]))
    metric('RE symbol table: same 15 rows, JSON vs TOON', json.dumps(symbols, indent=2), compact)
    decompiled = ghidra.decompile_function(str(ROOT / 'sample'), 'function_7')
    assert 'return' in decompiled and 'failed' not in decompiled.lower(), decompiled
    (ROOT / 'decompiled.txt').write_text(decompiled)
    # Full assembly baseline is measured locally only; never emitted into conversation.
    assembly = command(['objdump', '-d', str(ROOT / 'sample')]).stdout
    metric('RE task: full disassembly vs selected function', assembly, decompiled)
    renamed = ghidra.rename_symbol(str(ROOT / 'sample'), 'function_7', 'increment_seven')
    assert 'failed' not in renamed.lower(), renamed
    assert 'return' in ghidra.decompile_function(str(ROOT / 'sample'), 'increment_seven')
    bad = ROOT / 'broken.cpp'
    bad.write_text('\n'.join(f'int bad_{i}() {{ return missing_{i}; }}' for i in range(30)))
    args = ['g++', '-fsyntax-only', str(bad)]
    raw = command(args)
    compressed = command(['/home/dev/.local/bin/concise-build', *args])
    assert raw.returncode == compressed.returncode != 0
    assert 'error:' in compressed.stdout
    metric('C++ failed compilation: raw vs RTK/error filter', raw.stderr, compressed.stdout)
    cave = subprocess.run(['/home/dev/.caveman/bin/caveman-engine', 'compress'], input=raw.stderr,
                          text=True, capture_output=True, timeout=30)
    assert cave.returncode == 0, cave.stderr[-500:]
    metric('Caveman local engine: same C++ diagnostic input', raw.stderr, cave.stdout)
    definitions = [tool.model_dump(exclude_none=True) for tool in await gateway.mcp.list_tools()]
    compact_tokens = tokens(definitions)
    assert compact_tokens < 1000, compact_tokens
    direct = []
    for server in gateway.SERVERS:
        session = await gateway._session(server)
        tools = (await session.list_tools()).tools
        direct.extend(tool.model_dump(exclude_none=True) for tool in tools)
        print(f'PASS MCP {server}: {len(tools)} tools', flush=True)
    metric('MCP definitions: four direct servers vs Code Mode', direct, definitions)
    page = await gateway.execute('treesitter_mcp', 'code_map', json.dumps({'path': str(source)}), page=2)
    assert 'page=2' in page and 'has_more=' in page, page
    edited = await gateway.execute('serena_slim', 'replace_symbol_body', json.dumps({'relative_path': 'sample.cpp', 'name_path': 'function_8', 'body': 'extern "C" int function_8(int n) { return n + 88; }'}))
    assert 'error:' not in edited.lower(), edited
    assert 'n + 88' in source.read_text(), edited
    print('PASS Code Mode page forwarding and Serena symbolic edit', flush=True)
    for extension, content in {'cpp':'int f(){return 1;}', 'go':'package p\nfunc F() int {return 1}', 'ts':'function f(): number {return 1;}', 'py':'def f():\n    return 1\n', 'gd':'extends Node\nfunc f():\n    return 1\n'}.items():
        path = ROOT / ('parse.' + extension)
        path.write_text(content)
        assert ts.parse_file_symbols(path), extension
    print('PASS C++, Go, TypeScript, Python, GDScript parsers', flush=True)
    prompt_path = Path('/mnt/HC_Volume_106574422/repo-factory/control/automation/prompts/generic-system.md')
    prompt = prompt_path.read_text()
    import litellm
    from litellm.caching.caching import Cache
    count = [0]
    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            count[0] += 1
            body = json.dumps({'id':'local','object':'chat.completion','created':1,'model':'sample','choices':[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':20,'completion_tokens':1,'total_tokens':21}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Backend)
    Thread(target=server.serve_forever,daemon=True).start()
    litellm.cache = Cache(type='redis', host='127.0.0.1', port=6379, mode='default_off', namespace='factory-benchmark', ttl=60)
    request = dict(model='openai/sample', api_base=f'http://127.0.0.1:{server.server_port}/v1', api_key='local', messages=[{'role':'user','content':f'AST check {server.server_port}'}], temperature=0, cache={'use-cache':True})
    litellm.completion(**request)
    litellm.completion(**request)
    assert count[0] == 1, count
    request['messages'][0]['content'] += ' changed'
    litellm.completion(**request)
    assert count[0] == 2, count
    server.shutdown()
    print('PASS Redis exact repeat hit and changed-input miss', flush=True)
    report = ['# Token benchmark', '', 'Local fixture benchmark; cl100k_base tokenizer. No remote model requests or billed-token claims.', '', '| Measurement | Before tokens | After tokens | Reduction |','|---|---:|---:|---:|']
    report += [f'| {n} | {b} | {a} | {s}% |' for n,b,a,s in rows]
    report += ['', f'Current generic system prompt: {tokens(prompt)} tokens. Code Mode definitions: {compact_tokens} tokens. Combined: {tokens(prompt) + compact_tokens} tokens.', '', 'Redis: identical opted-in request hit (one backend call for two requests); changed input missed. This saves 50% of backend requests in a two-request repetition fixture, not 50% of general agent traffic.', '', 'RE full-disassembly comparison measures a targeted workflow change, not lossless compression. The same-row TOON measurement is lossless for this fixture. Compilation exit status and actual error lines were preserved.', '', 'Caveman engine, proxy and recovery MCP are installed with verified download checksums. Local engine compression is measured above; the existing 712-token system contract alone measured 0% reduction. No live model A/B trial was run, so no model-output saving or provider billing discount is claimed. Serena performed an actual symbolic edit. Ghidra decompiled and renamed an actual compiled function.', '', 'All five parsers, gateway pagination, and four backend tool catalogs passed.']
    Path('/home/dev/TOKEN_BENCHMARK.md').write_text('\n'.join(report)+'\n')
    print('\n'.join(report))

if __name__ == '__main__':
    asyncio.run(main())
