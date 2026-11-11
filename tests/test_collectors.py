import platform

import pytest

from mac_doctor_mcp.collectors import group_processes, parse_ps, stale_worktree_processes, inspect_storage, LinuxCollector

PS = """  PID  PPID   RSS COMMAND
  10     1  2048 /Applications/ego lite.app/Contents/MacOS/ego --type=renderer
  11     1  1024 /Applications/ego lite.app/Contents/MacOS/ego
  12     1  4096 /example/.hermes/hermes-agent/venv/bin/python run.py --token=SECRET
  13     1   512 node vite
"""


def test_group_and_hide_argv():
    rows = parse_ps(PS)
    groups = {g["group"]: g for g in group_processes(rows)}
    assert groups["EGO"]["processes"] == 2 and groups["EGO"]["relative_rss_bytes"] == 3072 * 1024
    assert groups["Hermes"]["processes"] == 1


def test_stale_worktree(tmp_path):
    (tmp_path / "repo/.worktrees/alive").mkdir(parents=True)
    rows = parse_ps(PS)
    cwd = {13: str(tmp_path / "repo/.worktrees/gone/app"), 12: str(tmp_path / "repo/.worktrees/alive")}
    out = stale_worktree_processes(rows, cwd.get)
    assert [(o["pid"], o["worktree"], o["name"]) for o in out] == [(13, "gone", "node")]


def test_inspect_storage(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/f").write_bytes(b"1" * 10000)
    (tmp_path / "b").write_text("x")
    out = inspect_storage([str(tmp_path)])
    assert out["roots"][0]["largest"][0]["path"] == str(tmp_path / "a")


@pytest.mark.skipif(platform.system() != "Linux", reason="linux only")
def test_linux_inspect_memory_runs():
    out = LinuxCollector().inspect_memory(top=3)
    assert out["memory"]["total_bytes"] > 0 and len(out["top_processes"]) <= 3
    assert all("SECRET" not in p["name"] for p in out["top_processes"])
