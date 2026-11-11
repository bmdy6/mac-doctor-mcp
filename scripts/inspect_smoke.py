"""Call only the three inspect tools through real stdio MCP; print a redacted summary.

Run from the checkout: uv run python scripts/inspect_smoke.py --root ./src
All server state lives in a temporary directory. No cleanup tool is called.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def summarize(name: str, data: dict) -> dict:
    if name == 'inspect_memory':
        result = dict(data)
        result['stale_worktree_processes'] = [
            {**p, 'worktree': f'<worktree-{i}>'}
            for i, p in enumerate(data['stale_worktree_processes'], 1)
        ]
        return result
    if name == 'inspect_storage':
        return {
            'ok': data['ok'],
            'roots': [{
                'root': f'<root-{i}>',
                **{key: root[key] for key in ('volume_free_bytes', 'volume_total_bytes', 'error') if key in root},
                'largest': [
                    {**{k: v for k, v in entry.items() if k != 'path'}, 'path': f'<entry-{j}>'}
                    for j, entry in enumerate(root.get('largest', []), 1)
                ],
            } for i, root in enumerate(data['roots'], 1)],
            'mole': {k: data['mole'][k] for k in ('available', 'reason', 'note') if k in data['mole']},
            'mole_analysis_count': len(data['mole'].get('analyses', [])),
        }
    result = {k: data[k] for k in ('ok', 'available', 'reason') if k in data}
    if 'report' in data:
        report = data['report']
        result['report'] = {k: v for k, v in report.items() if isinstance(v, (int, float, type(None))) or k == 'span'}
        result['report']['energy_by_app_count'] = len(report.get('energy_by_app', []))
    if 'snapshot' in data:
        snapshot = data['snapshot']
        result['snapshot'] = {k: snapshot[k] for k in (
            'schema_version', 'unix_ms', 'interval_ms', 'primed', 'temp', 'fans',
            'thermal_pressure', 'sys_power', 'cpu_power', 'gpu_power', 'battery', 'health',
        ) if k in snapshot}
    return result


async def main(root: str, output: str | None) -> None:
    root = str(Path(root).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix='mac-doctor-smoke-') as scratch:
        params = StdioServerParameters(
            command=sys.executable, args=['-m', 'mac_doctor_mcp.server'],
            env={**os.environ, 'MAC_DOCTOR_HOME': scratch + '/state',
                 'MAC_DOCTOR_CONFIG': scratch + '/config.json'},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                result = {
                    'captured_at': datetime.now(timezone.utc).isoformat(),
                    'transport': 'stdio MCP', 'tools': [t.name for t in tools.tools],
                    'redaction': 'Storage paths and worktree names replaced; hardware report and snapshot retain only metric fields; no argv, user, hostname, network or volume identifiers.',
                }
                for name, args in (
                    ('inspect_memory', {'top': 5}),
                    ('inspect_storage', {'roots': [root], 'top': 5}),
                    ('inspect_hardware', {}),
                ):
                    response = await client.call_tool(name, args, read_timeout_seconds=timedelta(seconds=180))
                    if response.isError:
                        raise RuntimeError(f'{name}: MCP call failed')
                    data = json.loads(next(c.text for c in response.content if c.type == 'text'))
                    if not data.get('ok'):
                        raise RuntimeError(f'{name}: {data.get("code", "not ok")}')
                    result[name] = summarize(name, data)
                    print(f'{name}: completed', file=sys.stderr)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if output:
        Path(output).write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='./src', help='Existing directory to inspect (read-only)')
    parser.add_argument('--output', help='Save the redacted JSON summary')
    args = parser.parse_args()
    asyncio.run(main(args.root, args.output))
