"""Read-only data collection. Platform-specific parts live behind ``Collector``.

- ``LinuxCollector``: /proc + ps, used for development and tests on Linux.
- ``MacCollector``: macOS memory probes, lsof cwd and optional siltop snapshots.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

from .txn import measure, TxnError

# (group name, substrings matched against the full command). Order matters: first match wins.
AI_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Claude Code", ("/claude", "claude-code", "/Claude.app/", "/@anthropic-ai/claude-code/")),
    ("Codex", ("/codex", "ChatGPT.app/", "Codex.app/", "/@openai/codex/")),
    ("EGO", ("ego lite.app/", "ego-browser")),
    ("Hermes", (".hermes/hermes-agent/",)),
    ("Cursor", ("Cursor.app/",)),
    ("Trae", ("Trae.app/",)),
    ("Feishu/Lark", ("Lark.app/", "Feishu.app/")),
    ("WeChat", ("WeChat.app/",)),
    ("Obsidian", ("Obsidian.app/",)),
)


def run(argv: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


MIB = 1024**2
GIB = 1024**3


class ProbeError(RuntimeError):
    """A read-only probe failed or returned an unsupported format."""


def run_checked(argv: list[str], timeout: float = 5.0, *, env: dict | None = None) -> str:
    """Do not mistake failed probes for empty, healthy measurements.

    Error messages omit raw stderr/argv because third-party tools can print secrets.
    """
    name = Path(argv[0]).name
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{name}: timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise ProbeError(f"{name}: {type(exc).__name__}") from exc
    if proc.returncode:
        raise ProbeError(f"{name}: exit {proc.returncode}")
    return proc.stdout


def bytes_from_number_unit(value: str, unit: str) -> int:
    scale = {"K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}[unit.upper()]
    return int(float(value) * scale)


def parse_memory_pressure(text: str) -> int:
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if not match:
        raise ProbeError("memory_pressure output missing free percentage")
    value = int(match.group(1))
    if not 0 <= value <= 100:
        raise ProbeError("memory_pressure percentage out of range")
    return value


def parse_vm_stat(text: str) -> dict[str, int | float]:
    page_match = re.search(r"page size of (\d+) bytes", text)
    if not page_match:
        raise ProbeError("vm_stat output missing page size")
    page_size = int(page_match.group(1))
    values: dict[str, int] = {}
    for raw in text.splitlines():
        match = re.match(r'([^:]+):\s+([0-9]+)\.?$', raw.strip())
        if match:
            values[match.group(1)] = int(match.group(2))
    required = ("Pages free", "Pages wired down", "Pages occupied by compressor")
    missing = [key for key in required if key not in values]
    if missing:
        raise ProbeError(f"vm_stat output missing: {', '.join(missing)}")
    return {
        "page_size_bytes": page_size,
        "free_bytes": values["Pages free"] * page_size,
        "wired_bytes": values["Pages wired down"] * page_size,
        "compressed_bytes": values["Pages occupied by compressor"] * page_size,
        "pageouts_since_boot": values.get("Pageouts", 0),
        "swapins_since_boot": values.get("Swapins", 0),
        "swapouts_since_boot": values.get("Swapouts", 0),
    }


def parse_swapusage(text: str) -> dict[str, int]:
    match = re.search(
        r"total\s*=\s*([0-9.]+)([KMGT])\s+used\s*=\s*([0-9.]+)([KMGT])\s+free\s*=\s*([0-9.]+)([KMGT])",
        text,
        re.I,
    )
    if not match:
        raise ProbeError("vm.swapusage output format not recognized")
    return {
        "total_bytes": bytes_from_number_unit(match.group(1), match.group(2)),
        "used_bytes": bytes_from_number_unit(match.group(3), match.group(4)),
        "free_bytes": bytes_from_number_unit(match.group(5), match.group(6)),
    }


def memory_risk(free_percent: int, swap_used: int, compressed: int, total: int) -> str:
    """doctor.py thresholds; swap alone can reflect historical pressure."""
    if free_percent < 10 or (free_percent < 20 and swap_used >= 4 * GIB):
        return "pressure"
    if free_percent < 25 or swap_used >= GIB or compressed >= total * 0.30:
        return "watch"
    return "healthy"


def probe_json(argv: list[str], *, timeout: float = 10.0, env: dict | None = None) -> dict:
    try:
        value = json.loads(run_checked(argv, timeout=timeout, env=env))
    except (ValueError, TypeError) as exc:
        raise ProbeError(f"{Path(argv[0]).name}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProbeError(f"{Path(argv[0]).name}: expected a JSON object")
    return value


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)$", line)
        if m:
            rows.append({"pid": int(m[1]), "ppid": int(m[2]), "rss_bytes": int(m[3]) * 1024, "command": m[4]})
    return rows


def group_processes(rows: list[dict]) -> list[dict]:
    """RSS is summed for relative comparison only: multi-process apps share memory."""
    groups: dict[str, dict] = {}
    for row in rows:
        for name, needles in AI_TOOL_GROUPS:
            if any(n in row["command"] for n in needles):
                g = groups.setdefault(name, {"group": name, "processes": 0, "relative_rss_bytes": 0})
                g["processes"] += 1
                g["relative_rss_bytes"] += row["rss_bytes"]
                break
    return sorted(groups.values(), key=lambda g: g["relative_rss_bytes"], reverse=True)


def stale_worktree_processes(rows: list[dict], cwd_of) -> list[dict]:
    """Processes whose cwd is inside a removed .worktrees/<name>. Detection only."""
    out = []
    for row in rows:
        cwd = cwd_of(row["pid"])
        if not cwd or "/.worktrees/" not in cwd:
            continue
        prefix, rest = cwd.split("/.worktrees/", 1)
        name = rest.split("/", 1)[0]
        if name and not os.path.exists(f"{prefix}/.worktrees/{name}"):
            out.append({"pid": row["pid"], "worktree": name, "rss_bytes": row["rss_bytes"],
                        "name": os.path.basename(row["command"].split()[0])})
    return out


def safe_name(command: str) -> str:
    """Only the executable name, never full argv (may contain tokens)."""
    return os.path.basename(command.split()[0]) if command.split() else "?"


class Collector:
    platform = "unknown"

    def processes(self) -> list[dict]:
        return parse_ps(run(["ps", "-axo", "pid,ppid,rss,command"]))

    def cwd_of(self, pid: int) -> str | None:
        return None

    def memory(self) -> dict:
        raise NotImplementedError

    def hardware(self) -> dict:
        return {"available": False, "reason": "not supported on this platform"}

    def inspect_memory(self, top: int = 10) -> dict:
        rows = self.processes()
        top_rows = sorted(rows, key=lambda r: r["rss_bytes"], reverse=True)[:top]
        return {
            "platform": self.platform,
            "memory": self.memory(),
            "ai_tools": group_processes(rows),
            "top_processes": [{"pid": r["pid"], "name": safe_name(r["command"]), "rss_bytes": r["rss_bytes"]} for r in top_rows],
            "stale_worktree_processes": stale_worktree_processes(rows, self.cwd_of),
            "note": "RSS sums are relative; shared memory is counted more than once.",
        }


class LinuxCollector(Collector):
    platform = "linux"

    def cwd_of(self, pid: int) -> str | None:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None

    def memory(self) -> dict:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            parts = value.split()
            if parts:
                info[key] = int(parts[0]) * 1024
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        swap_used = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
        free_pct = round(avail * 100 / total) if total else None
        risk = "pressure" if free_pct is not None and free_pct < 10 else "watch" if (free_pct or 100) < 25 or swap_used > total * 0.25 else "healthy"
        return {"total_bytes": total, "available_bytes": avail, "swap_used_bytes": swap_used, "free_percent": free_pct, "risk": risk}


class MacCollector(Collector):
    platform = "macos"

    def cwd_of(self, pid: int) -> str | None:
        output = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=2.0)
        return next((line[1:] for line in output.splitlines() if line.startswith("n/")), None)

    def memory(self) -> dict:
        try:
            total = int(run_checked(["sysctl", "-n", "hw.memsize"]).strip())
        except ValueError as exc:
            raise ProbeError("sysctl: invalid hw.memsize") from exc
        if total <= 0:
            raise ProbeError("sysctl: invalid hw.memsize")
        free_percent = parse_memory_pressure(run_checked(["memory_pressure", "-Q"]))
        vm = parse_vm_stat(run_checked(["vm_stat"]))
        swap = parse_swapusage(run_checked(["sysctl", "vm.swapusage"]))
        return {
            "total_bytes": total, "available_bytes": vm["free_bytes"],
            "swap_used_bytes": swap["used_bytes"], "free_percent": free_percent,
            "risk": memory_risk(free_percent, swap["used_bytes"], vm["compressed_bytes"], total),
        }

    def hardware(self) -> dict:
        binary = shutil.which("siltop")
        if not binary:
            return {"available": False, "reason": "siltop not installed"}
        result = {}
        errors = []
        for key, argv in (("report", [binary, "report", "today", "--json"]),
                          ("snapshot", [binary, "--json"])):
            try:
                result[key] = probe_json(argv)
            except ProbeError as exc:
                errors.append(f"{key}: {exc}")
        return {"available": not errors, **result, **({"reason": "; ".join(errors)} if errors else {})}


def get_collector() -> Collector:
    return MacCollector() if platform.system() == "Darwin" else LinuxCollector()


def inspect_storage(roots: list[str], top: int = 15) -> dict:
    """Biggest direct children of each root, plus free space. Read-only."""
    result = []
    for raw in roots:
        root = Path(raw).expanduser()
        if not root.is_dir() or root.is_symlink():
            result.append({"root": str(root), "error": "not a real directory"})
            continue
        children = []
        for entry in os.scandir(root):
            try:
                size, entries = measure(Path(entry.path))
                children.append({"path": entry.path, "bytes": size, "entries": entries})
            except (TxnError, OSError) as exc:
                children.append({"path": entry.path, "bytes": None, "skipped": getattr(exc, "code", type(exc).__name__)})
        children.sort(key=lambda c: c["bytes"] or 0, reverse=True)
        usage = shutil.disk_usage(root)
        result.append({"root": str(root), "volume_free_bytes": usage.free, "volume_total_bytes": usage.total, "largest": children[:top]})
    return {"roots": result, "mole": mole_candidates([r["root"] for r in result if "error" not in r])}


def mole_candidates(roots: list[str]) -> dict:
    """Only headless analyze JSON. Never fall back to the TUI or a clean command.

    Mole may prune its own analyzer cache at startup. Isolate its home/cache in
    a temporary directory so even this housekeeping cannot touch user state.
    """
    binary = shutil.which("mo")
    if not binary:
        return {"available": False, "reason": "Mole (mo) not installed"}
    analyses = []
    with tempfile.TemporaryDirectory(prefix="mac-doctor-mole-") as scratch:
        env = {**os.environ, "HOME": scratch, "XDG_CACHE_HOME": scratch + "/cache",
               "XDG_CONFIG_HOME": scratch + "/config", "TMPDIR": scratch,
               "MO_ANALYZE_PATH": "", "MOLE_DRY_RUN": "1"}
        try:
            help_text = run_checked([binary, "analyze", "--help"], env=env)
            if "--json" not in help_text:
                raise ProbeError("Mole analyze --json not supported; skipped")
        except ProbeError as exc:
            return {"available": False, "reason": str(exc)}
        for raw in roots:
            root = str(Path(raw).expanduser().absolute())
            try:
                data = probe_json([binary, "analyze", "--json", root], timeout=30.0, env=env)
                if not isinstance(data.get("entries"), list):
                    raise ProbeError("Mole: missing analysis entries")
                analyses.append({"root": root, "data": data})
            except ProbeError as exc:
                analyses.append({"root": root, "reason": str(exc)})
    available = bool(analyses) and all("data" in item for item in analyses)
    return {"available": available, "analyses": analyses,
            "note": "Analysis entries are review candidates, not permission to clean. Check scan_status; partial sizes are lower bounds.",
            **({"reason": "No valid roots or one or more analyses failed"} if not available else {})}
