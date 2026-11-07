"""Real LiteLLM Router tests against local responses; no provider credits used."""
import asyncio
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

os.environ['FACTORY_DRYRUN_ECHO'] = '1'
os.environ['FACTORY_NOW'] = '2026-11-06T00:00:00Z'
import litellm
import yaml
import litellm_callbacks as cb

CONFIG = yaml.safe_load(Path(__file__).with_name('litellm.experimental.yaml').read_text())

async def route(name, blocked=(), timeout=False, now='2026-11-06T00:00:00Z', expected='codex-cli'):
    cfg = copy.deepcopy(CONFIG)
    for entry in cfg['model_list']:
        params = entry['litellm_params']
        if params['model'].startswith('deepseek/'):
            params['mock_response'] = 'LOCAL_DEEPSEEK_OK'
            params['api_key'] = 'offline-not-a-key'
    os.environ['FACTORY_NOW'] = now
    with tempfile.TemporaryDirectory() as directory:
        cb.TIER_STATE_PATH = Path(directory) / 'state.json'
        prompt = ''.join(f'[SIMULATE_TIER{tier}_429]' for tier in blocked)
        if timeout:
            prompt += '[SIMULATE_TIER2_TIMEOUT]'
        router = litellm.Router(model_list=cfg['model_list'], **cfg['router_settings'])
        response = await router.acompletion(model='tier0a-openai', messages=[{'role': 'user', 'content': prompt or 'OK'}])
        assert expected in response.model, (name, response.model)
        print(f'PASS {name}: {response.model}')
        router.reset()

def compression_checks():
    messages = [{'role': 'system', 'content': 'contract'}, {'role': 'user', 'content': 'Fix the C++ parser'}]
    for i in range(12):
        messages += [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': str(i), 'type': 'function', 'function': {'name': 'compile', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': str(i), 'content': 'src/parser.cpp error: missing semicolon'}]
    short = cb.compress_conversation_history(messages, keep_recent_messages=3)
    ids = {t['id'] for m in short for t in m.get('tool_calls', [])}
    assert all(m['tool_call_id'] in ids for m in short if m['role'] == 'tool')
    assert len(short) < len(messages)
    for _ in range(12):
        short += [{'role': 'assistant', 'content': 'done'}, {'role': 'user', 'content': 'continue'}]
    again = cb.compress_conversation_history(short)
    assert 'Architectural Goal: Fix the C++ parser' in str(again)
    original = [{'role': 'system', 'content': [{'type': 'text', 'text': 'goal'}]}]
    cb.inject_prompt_cache_headers(original)
    assert 'cache_control' not in original[0]['content'][0]
    print('PASS tool-pair integrity, repeated compaction, cache copy isolation')

async def main():
    await route('0A active')
    await route('0A quota', ('0A',), expected='claude-cli')
    await route('0A+0B quota', ('0A', '0B'), expected='agy')
    await route('0A+0B+1 quota', ('0A', '0B', '1'), expected='opencode-go')
    await route('all subscription quotas', ('0A', '0B', '1', '2'), expected='deepseek')
    await route('worker timeout', ('0A', '0B', '1'), timeout=True, expected='deepseek')
    await route('0A inclusive final second', now='2026-11-16T23:59:59Z')
    await route('0A expired', now='2026-11-17T00:00:00Z', expected='claude-cli')
    await route('0B inclusive final second', now='2026-12-01T23:59:59Z', expected='claude-cli')
    await route('both expired', now='2026-12-02T00:00:00Z', expected='agy')
    compression_checks()
    data = {'model': 'tier0a-openai', 'messages': [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}], 'cache': {'use-cache': True}}
    await cb.token_efficiency_callback.async_pre_call_hook(None, None, data, 'acompletion')
    assert data['cache'] == {'no-cache': True, 'no-store': True}
    print('PASS multi-turn cache bypass')
    os.environ['FACTORY_DRYRUN_ECHO'] = '0'
    tool = {'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object', 'properties': {}}}}
    messages = [{'role': 'user', 'content': 'Look up the symbol'}]
    expected = litellm.ModelResponse(model='sample', choices=[{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}}])
    with tempfile.TemporaryDirectory() as directory:
        cb.TIER_STATE_PATH = Path(directory) / 'state.json'
        with patch.object(litellm, 'completion', return_value=expected) as transport:
            result = cb.opencode_go_handler.completion('deepseek-v4.1-flash', messages, api_key='local', optional_params={'tools': [tool]})
            assert transport.call_args.kwargs['tools'] == [tool]
            assert transport.call_args.kwargs['messages'] == messages
            assert result.choices[0].message.tool_calls[0].id == 'call1'
    print('PASS OpenCode tool definitions, response tool calls and IDs preserved')

if __name__ == '__main__':
    asyncio.run(main())
