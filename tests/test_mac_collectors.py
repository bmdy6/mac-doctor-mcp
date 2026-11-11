import json
import subprocess

import pytest

from mac_doctor_mcp import collectors as c

GIB = 1024**3
VM = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 100.
Pages wired down: 200.
Pages occupied by compressor: 300.
Pageouts: 4.
Swapins: 5.
Swapouts: 6.
'''


def test_memory_parsers():
    assert c.parse_memory_pressure('System-wide memory free percentage: 52%\n') == 52
    assert c.parse_vm_stat(VM) == {
        'page_size_bytes': 16384, 'free_bytes': 100 * 16384,
        'wired_bytes': 200 * 16384, 'compressed_bytes': 300 * 16384,
        'pageouts_since_boot': 4, 'swapins_since_boot': 5, 'swapouts_since_boot': 6,
    }
    assert c.parse_swapusage('vm.swapusage: total = 2.00G used = 512.50M free = 1.50G (encrypted)') == {
        'total_bytes': 2 * GIB, 'used_bytes': int(512.5 * 1024**2), 'free_bytes': int(1.5 * GIB),
    }


@pytest.mark.parametrize('parser,text', [
    ('parse_memory_pressure', ''), ('parse_memory_pressure', 'System-wide memory free percentage: 101%'),
    ('parse_vm_stat', ''), ('parse_vm_stat', 'page size of 16384 bytes\nPages free: 1.'),
    ('parse_swapusage', ''),
])
def test_bad_memory_output(parser, text):
    with pytest.raises(c.ProbeError):
        getattr(c, parser)(text)


@pytest.mark.parametrize('free,swap,compressed,risk', [
    (9, 0, 0, 'pressure'), (10, 0, 0, 'watch'),
    (19, 4 * GIB, 0, 'pressure'), (20, 4 * GIB, 0, 'watch'),
    (24, 0, 0, 'watch'), (25, 0, 0, 'healthy'),
    (60, GIB, 0, 'watch'), (60, 0, 3 * GIB, 'watch'),
    (60, 128 * 1024**2, GIB, 'healthy'),
])
def test_risk_thresholds(free, swap, compressed, risk):
    assert c.memory_risk(free, swap, compressed, 10 * GIB) == risk


def test_mac_memory_contract(monkeypatch):
    outputs = {
        ('sysctl', '-n', 'hw.memsize'): str(24 * GIB),
        ('memory_pressure', '-Q'): 'System-wide memory free percentage: 52%',
        ('vm_stat',): VM,
        ('sysctl', 'vm.swapusage'): 'total = 2G used = 1G free = 1G',
    }
    monkeypatch.setattr(c, 'run_checked', lambda argv, **kw: outputs[tuple(argv)])
    assert c.MacCollector().memory() == {
        'total_bytes': 24 * GIB, 'available_bytes': 100 * 16384,
        'swap_used_bytes': GIB, 'free_percent': 52, 'risk': 'watch',
    }


def test_mac_cwd(monkeypatch):
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        return 'p123\nfcwd\nn/repo with spaces/.worktrees/gone\n'
    monkeypatch.setattr(c, 'run', fake)
    assert c.MacCollector().cwd_of(123) == '/repo with spaces/.worktrees/gone'
    assert calls == [['lsof', '-a', '-p', '123', '-d', 'cwd', '-Fn']]
    monkeypatch.setattr(c, 'run', lambda *a, **kw: '')
    assert c.MacCollector().cwd_of(123) is None


def test_mac_app_paths():
    paths = {
        'EGO': '/Applications/ego lite.app/Contents/MacOS/ego lite',
        'Codex': '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT',
        'Claude Code': '/example/.local/share/claude/versions/1.0',
        'Cursor': '/Applications/Cursor.app/Contents/MacOS/Cursor',
        'Trae': '/Applications/Trae.app/Contents/MacOS/Trae',
        'Feishu/Lark': '/Applications/Lark.app/Contents/MacOS/Lark',
        'WeChat': '/Applications/WeChat.app/Contents/MacOS/WeChat',
        'Obsidian': '/Applications/Obsidian.app/Contents/MacOS/Obsidian',
        'Hermes': '/example/.hermes/hermes-agent/venv/bin/python',
    }
    rows = [{'command': p, 'rss_bytes': 100} for p in paths.values()]
    assert {g['group'] for g in c.group_processes(rows)} == paths.keys()


def test_hardware_both_samples(monkeypatch):
    monkeypatch.setattr(c.shutil, 'which', lambda name: '/opt/bin/' + name)
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        return json.dumps({'recorded_s': 0} if 'report' in argv else {'temp': {'cpu_temp_avg': 50}})
    monkeypatch.setattr(c, 'run_checked', fake)
    result = c.MacCollector().hardware()
    assert result['available'] is True
    assert result['report'] == {'recorded_s': 0}
    assert result['snapshot']['temp']['cpu_temp_avg'] == 50
    assert calls == [['/opt/bin/siltop', 'report', 'today', '--json'], ['/opt/bin/siltop', '--json']]


@pytest.mark.parametrize('output', ['not json', '[]', 'null'])
def test_hardware_bad_json(monkeypatch, output):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/siltop')
    monkeypatch.setattr(c, 'run_checked', lambda *a, **kw: output)
    result = c.MacCollector().hardware()
    assert result['available'] is False and result['reason']


def test_missing_optional_tools(monkeypatch):
    monkeypatch.setattr(c.shutil, 'which', lambda _: None)
    assert c.MacCollector().hardware()['available'] is False
    assert c.mole_candidates([])['available'] is False


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'missing'])
def test_checked_command_failure(monkeypatch, failure):
    def fake(argv, **kw):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == 'missing':
            raise FileNotFoundError()
        return subprocess.CompletedProcess(argv, 1, stdout='{}', stderr='private detail')
    monkeypatch.setattr(c.subprocess, 'run', fake)
    with pytest.raises(c.ProbeError):
        c.run_checked(['probe'])


def test_memory_probe_error_is_structured(monkeypatch):
    from mac_doctor_mcp import server
    monkeypatch.setattr(server, 'get_collector', c.MacCollector)
    def fail(*args, **kwargs):
        raise c.ProbeError('probe unavailable')
    monkeypatch.setattr(c, 'run_checked', fail)
    assert server.inspect_memory()['code'] == 'E_PROBE'


def test_mole_analyze_only(monkeypatch, tmp_path):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/mo')
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        if '--help' in argv:
            return 'Usage: mo analyze [OPTIONS] [PATH]\n --json Output analysis as JSON'
        assert kw['env']['HOME'] != str(c.Path.home())
        return json.dumps({'scan_status': 'partial', 'entries': [
            {'path': str(tmp_path / 'cache'), 'size': 42, 'scan_status': 'complete'},
        ]})
    monkeypatch.setattr(c, 'run_checked', fake)
    result = c.inspect_storage([str(tmp_path)])
    assert result['mole']['available'] is True
    scan = result['mole']['analyses'][0]
    assert scan['data']['scan_status'] == 'partial'
    assert scan['data']['entries'][0]['size'] == 42
    assert calls == [['/bin/mo', 'analyze', '--help'], ['/bin/mo', 'analyze', '--json', str(tmp_path)]]


def test_mole_unsupported_has_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/mo')
    calls = []
    def fake(argv, **kw):
        calls.append(argv)
        return 'interactive analyzer'
    monkeypatch.setattr(c, 'run_checked', fake)
    assert c.mole_candidates([str(tmp_path)])['available'] is False
    assert calls == [['/bin/mo', 'analyze', '--help']]


@pytest.mark.parametrize('output', ['garbage', '[]', '{}'])
def test_mole_bad_json(monkeypatch, tmp_path, output):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/mo')
    monkeypatch.setattr(c, 'run_checked', lambda argv, **kw: '--json' if '--help' in argv else output)
    result = c.mole_candidates([str(tmp_path)])
    assert result['available'] is False
    assert result['analyses'][0]['reason']


@pytest.mark.parametrize('failed_key', ['report', 'snapshot'])
def test_hardware_preserves_partial_success(monkeypatch, failed_key):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/siltop')
    def fake(argv, **kw):
        key = 'report' if 'report' in argv else 'snapshot'
        if key == failed_key:
            raise c.ProbeError('siltop: timed out')
        return '{"sample": 1}'
    monkeypatch.setattr(c, 'run_checked', fake)
    result = c.MacCollector().hardware()
    assert result['available'] is False
    assert failed_key in result['reason']
    assert result['snapshot' if failed_key == 'report' else 'report'] == {'sample': 1}


def test_mole_failure_does_not_break_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(c.shutil, 'which', lambda _: '/bin/mo')
    def fake(argv, **kw):
        if '--help' in argv:
            return '--json'
        raise c.ProbeError('mo: timed out')
    monkeypatch.setattr(c, 'run_checked', fake)
    result = c.inspect_storage([str(tmp_path)])
    assert result['roots'][0]['volume_total_bytes'] > 0
    assert result['mole']['available'] is False
    assert result['mole']['analyses'][0]['reason'] == 'mo: timed out'
