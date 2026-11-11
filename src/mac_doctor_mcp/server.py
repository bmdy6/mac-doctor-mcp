"""MCP server. Read tools never change anything; apply/rollback/purge do and say so."""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .collectors import ProbeError, get_collector, inspect_storage as _inspect_storage
from .policy import Policy, PolicyError
from .txn import Quarantine, TxnError

STATE_HOME = Path(os.environ.get("MAC_DOCTOR_HOME", "~/.mac-doctor")).expanduser()
CONFIG = Path(os.environ.get("MAC_DOCTOR_CONFIG", "~/.config/mac-doctor/config.json")).expanduser()

mcp = FastMCP("mac-doctor")
_q: Quarantine | None = None


def q() -> Quarantine:
    global _q
    if _q is None:
        _q = Quarantine(STATE_HOME, Policy.load(CONFIG))
    return _q


def _err(exc: Exception) -> dict:
    return {"ok": False, "code": getattr(exc, "code", type(exc).__name__), "detail": getattr(exc, "detail", str(exc))}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def inspect_memory(top: int = 10) -> dict:
    """Read-only. Memory pressure, swap, processes grouped by AI tool, and dev processes
    left running in deleted git worktrees. Does not kill anything."""
    try:
        return {"ok": True, **get_collector().inspect_memory(top=max(1, min(top, 50)))}
    except ProbeError as exc:
        return {"ok": False, "code": "E_PROBE", "detail": str(exc)}
    except NotImplementedError as exc:
        return {"ok": False, "code": "E_NOT_IMPLEMENTED", "detail": str(exc)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def inspect_storage(roots: list[str] | None = None, top: int = 15) -> dict:
    """Read-only. Largest items under the given folders (default: the allowed cleanup roots)."""
    policy = q().policy
    roots = roots or [str(p) for p in policy.allow_roots + policy.artifact_roots if p.is_dir()]
    return {"ok": True, **_inspect_storage(roots, top=max(1, min(top, 100)))}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def inspect_hardware() -> dict:
    """Read-only. Temperatures, power and per-app energy from siltop when installed (macOS)."""
    try:
        return {"ok": True, **get_collector().hardware()}
    except NotImplementedError as exc:
        return {"ok": False, "code": "E_NOT_IMPLEMENTED", "detail": str(exc)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def plan_cleanup(items: list[dict]) -> dict:
    """Read-only on target files; writes plan metadata locally. Turn candidates into a stored plan.
    items: [{"path": "/abs/path", "reason": "why it is safe", "risk": "low|medium|high"}].
    Show the user every item, the total size and the confirm_code, and ask them to reply
    with the code. Never apply without the user typing it."""
    try:
        plan = q().plan(items)
    except (TxnError, PolicyError) as exc:
        return _err(exc)
    return {"ok": True, **{k: v for k, v in plan.items() if k != "schema"}}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def apply_plan(plan_id: str, confirm_code: str) -> dict:
    """CHANGES FILES (可回滚 / reversible). Moves the plan's items into quarantine on the same volume.
    confirm_code must be the code the user typed. Space is only freed after purge_quarantine."""
    try:
        return {"ok": True, **q().apply(plan_id, confirm_code)}
    except (TxnError, PolicyError, OSError) as exc:
        return _err(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def rollback(plan_id: str) -> dict:
    """CHANGES FILES (可回滚 / restores quarantined files). Moves every item of an applied plan back to where it was."""
    try:
        return {"ok": True, **q().rollback(plan_id)}
    except (TxnError, OSError) as exc:
        return _err(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def recover() -> dict:
    """CHANGES FILES (可回滚 / restores interrupted moves). Finishes an apply or rollback that was interrupted, by restoring items."""
    try:
        return {"ok": True, **q().recover()}
    except (TxnError, OSError) as exc:
        return _err(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def list_quarantine() -> dict:
    """Read-only. Applied, rolled back and purged plans with sizes."""
    return {"ok": True, "batches": q().list_quarantine()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def purge_quarantine(keep_days: int = 7, confirm_code: str | None = None) -> dict:
    """不可回滚 / NOT REVERSIBLE when confirm_code is given. Without it: dry run listing batches older than
    keep_days and a confirm_code. Show the list to the user; only pass the code they typed."""
    try:
        return {"ok": True, **q().purge(keep_days=max(1, keep_days), confirm_code=confirm_code)}
    except (TxnError, OSError) as exc:
        return _err(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
