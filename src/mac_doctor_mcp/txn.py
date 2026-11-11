"""Cleanup as a reversible transaction.

Nothing is deleted on apply. Each planned item is *renamed* into a quarantine
folder on the same volume, so apply and rollback are instant and keep the same
inode. A journal (``state.json`` + per-plan batch file) is written before and
after every move, so an interrupted apply or rollback can be finished by
``recover``. Only ``purge`` really deletes, and only quarantined batches.

Transaction ideas (lock, pending journal, atomic JSON, audit log, recover)
come from deploy-gate.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import time
import uuid

from .policy import Policy, PolicyError, denied

SCHEMA = 1
PLAN_TTL_S = 24 * 3600
DEFAULT_KEEP_DAYS = 7
MAX_WALK_ENTRIES = 2_000_000


class TxnError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def atomic_json(path: Path, value) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, canonical(value))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path):
    if path.is_symlink():
        raise TxnError("E_UNSAFE_STATE", str(path))
    return json.loads(path.read_text("utf-8"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity(info: os.stat_result) -> dict:
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
    out = {"dev": info.st_dev, "ino": info.st_ino, "kind": kind}
    if kind == "file":
        out.update(size=info.st_size, mtime_ns=info.st_mtime_ns)
    return out


def same_object(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in ("dev", "ino", "kind", "size", "mtime_ns"))


def measure(path: Path) -> tuple[int, int]:
    """Disk bytes and entry count; rejects symlinked-out or denied content inside."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise TxnError("E_SYMLINK_ITEM", str(path))
    if not stat.S_ISDIR(info.st_mode):
        if not stat.S_ISREG(info.st_mode):
            raise TxnError("E_SPECIAL_FILE", str(path))
        return _disk_bytes(info), 1
    total, count = _disk_bytes(info), 1
    stack = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > MAX_WALK_ENTRIES:
                    raise TxnError("E_TOO_MANY_ENTRIES", str(path))
                try:
                    denied(Path(entry.path).relative_to(path).parts)
                except PolicyError as exc:
                    raise TxnError("E_DENIED_INSIDE", entry.path) from exc
                child = entry.stat(follow_symlinks=False)
                total += _disk_bytes(child)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    return total, count


def _disk_bytes(info: os.stat_result) -> int:
    blocks = getattr(info, "st_blocks", None)
    return blocks * 512 if blocks is not None else info.st_size


class Quarantine:
    """State home layout: lock, state.json, audit.jsonl, plans/, batches/, items/<plan_id>/"""

    def __init__(self, home: Path, policy: Policy, *, clock=time.time):
        self.home = Path(home).absolute()
        self.policy = policy
        self.clock = clock
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("plans", "batches", "items"):
            (self.home / name).mkdir(mode=0o700, exist_ok=True)
        (self.home / "lock").touch(mode=0o600)
        if not (self.home / "state.json").exists():
            atomic_json(self.home / "state.json", {"schema": SCHEMA, "pending": None})
        self.dev = self.home.stat().st_dev

    # ---------- infrastructure ----------
    @contextmanager
    def locked(self):
        fd = os.open(self.home / "lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TxnError("E_LOCKED") from exc
            yield
        finally:
            os.close(fd)

    def _state(self) -> dict:
        state = read_json(self.home / "state.json")
        if not isinstance(state, dict) or state.get("schema") != SCHEMA:
            raise TxnError("E_INVALID_STATE")
        return state

    def _set_pending(self, pending) -> None:
        atomic_json(self.home / "state.json", {"schema": SCHEMA, "pending": pending})

    def _require_idle(self) -> None:
        if self._state()["pending"] is not None:
            raise TxnError("E_RECOVERY_REQUIRED", "run recover first")

    def _audit(self, operation: str, outcome: str, plan_id: str | None = None, **extra) -> None:
        fd = os.open(self.home / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, canonical({"ts": int(self.clock()), "operation": operation, "plan_id": plan_id, "outcome": outcome, **extra}))
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _check_id(plan_id) -> str:
        if not (isinstance(plan_id, str) and len(plan_id) == 32 and all(c in "0123456789abcdef" for c in plan_id)):
            raise TxnError("E_PLAN_ID")
        return plan_id

    def _batch_path(self, plan_id: str) -> Path:
        return self.home / "batches" / f"{self._check_id(plan_id)}.json"

    # ---------- plan ----------
    def plan(self, items: list[dict]) -> dict:
        """items: [{"path": abs path, "reason": str, "risk": low|medium|high}] -> stored plan.

        Read-only on the target paths. Rejected items are returned with a code, not raised.
        """
        if not items:
            raise TxnError("E_EMPTY_PLAN")
        accepted, rejected, seen = [], [], set()
        for raw in items:
            path = raw.get("path") if isinstance(raw, dict) else None
            try:
                checked = self.policy.check(path)
                home_s = str(self.home)
                if str(checked) == home_s or str(checked).startswith(home_s + "/") or home_s.startswith(str(checked) + "/"):
                    raise TxnError("E_STATE_HOME", path)
                if any(str(checked) == s or str(checked).startswith(s + "/") or s.startswith(str(checked) + "/") for s in seen):
                    raise TxnError("E_OVERLAPPING_ITEMS", path)
                info = checked.lstat()
                if info.st_dev != self.dev:
                    raise TxnError("E_CROSS_DEVICE", "quarantine must be on the same volume")
                size, entries = measure(checked)
                seen.add(str(checked))
                accepted.append({
                    "index": len(accepted), "path": str(checked), "reason": str(raw.get("reason", ""))[:300],
                    "risk": raw.get("risk") if raw.get("risk") in {"low", "medium", "high"} else "medium",
                    "bytes": size, "entries": entries, "identity": identity(info),
                })
            except FileNotFoundError:
                rejected.append({"path": path, "code": "E_MISSING"})
            except (PolicyError, TxnError) as exc:
                rejected.append({"path": path, "code": exc.code})
        if not accepted:
            return {"plan_id": None, "items": [], "rejected": rejected, "total_bytes": 0}
        plan = {
            "schema": SCHEMA, "plan_id": uuid.uuid4().hex, "created_at": int(self.clock()),
            "confirm_code": secrets.token_hex(3), "items": accepted, "rejected": rejected,
            "total_bytes": sum(i["bytes"] for i in accepted),
        }
        atomic_json(self.home / "plans" / f"{plan['plan_id']}.json", plan)
        self._audit("plan", "created", plan["plan_id"], items=len(accepted), bytes=plan["total_bytes"])
        return plan

    def _load_plan(self, plan_id: str) -> dict:
        path = self.home / "plans" / f"{self._check_id(plan_id)}.json"
        if not path.exists():
            raise TxnError("E_UNKNOWN_PLAN")
        plan = read_json(path)
        digest = hashlib.sha256(canonical(plan)).hexdigest()
        return {**plan, "_digest": digest}

    # ---------- apply ----------
    def apply(self, plan_id: str, confirm_code: str) -> dict:
        with self.locked():
            self._require_idle()
            plan = self._load_plan(plan_id)
            if self._batch_path(plan_id).exists():
                raise TxnError("E_PLAN_ALREADY_USED")
            if not secrets.compare_digest(str(confirm_code), plan["confirm_code"]):
                self._audit("apply", "rejected:E_CONFIRM", plan_id)
                raise TxnError("E_CONFIRM", "confirm_code must come from the user")
            if self.clock() - plan["created_at"] > PLAN_TTL_S:
                raise TxnError("E_PLAN_EXPIRED", "make a new plan")
            # Re-check every item before touching anything.
            for item in plan["items"]:
                self.policy.check(item["path"])
                try:
                    now = Path(item["path"]).lstat()
                except FileNotFoundError as exc:
                    raise TxnError("E_ITEM_CHANGED", item["path"]) from exc
                if not same_object(identity(now), item["identity"]):
                    raise TxnError("E_ITEM_CHANGED", item["path"])
            target_root = self.home / "items" / plan_id
            target_root.mkdir(mode=0o700)
            batch = {"schema": SCHEMA, "plan_id": plan_id, "plan_digest": plan["_digest"], "status": "applying",
                     "applied_at": None, "items": [{"index": i["index"], "path": i["path"], "identity": i["identity"],
                                                     "bytes": i["bytes"], "status": "planned"} for i in plan["items"]]}
            atomic_json(self._batch_path(plan_id), batch)
            self._set_pending({"op": "apply", "plan_id": plan_id})
            try:
                for item in batch["items"]:
                    dest = target_root / str(item["index"])
                    os.rename(item["path"], dest)
                    item["status"] = "moved"
                    atomic_json(self._batch_path(plan_id), batch)
                    if not same_object(identity(dest.lstat()), item["identity"]):
                        raise TxnError("E_MOVED_OBJECT_MISMATCH", item["path"])
            except Exception as exc:
                try:
                    self._rollback_items(batch, target_root)
                except Exception as restore_error:
                    self._audit("apply", "recovery_required", plan_id)
                    raise TxnError("E_RECOVERY_REQUIRED", getattr(restore_error, "code", str(restore_error))) from restore_error
                batch["status"] = "failed"
                atomic_json(self._batch_path(plan_id), batch)
                self._set_pending(None)
                self._audit("apply", "rolled_back", plan_id, error=getattr(exc, "code", type(exc).__name__))
                raise TxnError("E_APPLY_ROLLED_BACK", getattr(exc, "code", str(exc))) from exc
            batch["status"] = "applied"
            batch["applied_at"] = int(self.clock())
            atomic_json(self._batch_path(plan_id), batch)
            self._set_pending(None)
            freed = sum(i["bytes"] for i in batch["items"])
            self._audit("apply", "applied", plan_id, bytes=freed)
            return {"plan_id": plan_id, "status": "applied", "moved": len(batch["items"]), "bytes": freed,
                    "note": "files are in quarantine; space is freed after purge"}

    # ---------- rollback ----------
    def rollback(self, plan_id: str) -> dict:
        with self.locked():
            self._require_idle()
            path = self._batch_path(plan_id)
            if not path.exists():
                raise TxnError("E_UNKNOWN_BATCH")
            batch = read_json(path)
            if batch["status"] != "applied":
                raise TxnError("E_NOT_ROLLBACKABLE", batch["status"])
            self._set_pending({"op": "rollback", "plan_id": plan_id})
            try:
                self._rollback_items(batch, self.home / "items" / plan_id)
            except Exception as exc:
                atomic_json(path, batch)
                self._audit("rollback", "recovery_required", plan_id)
                raise TxnError("E_RECOVERY_REQUIRED", getattr(exc, "code", str(exc))) from exc
            batch["status"] = "rolled_back"
            atomic_json(path, batch)
            self._set_pending(None)
            self._audit("rollback", "restored", plan_id)
            return {"plan_id": plan_id, "status": "rolled_back", "restored": len(batch["items"])}

    def _rollback_items(self, batch: dict, target_root: Path) -> None:
        """Move every 'moved' item back, newest first. Saves the journal after each step."""
        for item in reversed(batch["items"]):
            if item["status"] != "moved":
                continue
            src = target_root / str(item["index"])
            original = Path(item["path"])
            if os.path.lexists(original):
                raise TxnError("E_RESTORE_TARGET_EXISTS", str(original))
            if not same_object(identity(src.lstat()), item["identity"]):
                raise TxnError("E_QUARANTINE_TAMPERED", str(src))
            original.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, original)
            item["status"] = "restored"
            atomic_json(self._batch_path(batch["plan_id"]), batch)

    # ---------- recover ----------
    def recover(self) -> dict:
        """Finish an interrupted apply (by rolling it back) or an interrupted rollback."""
        with self.locked():
            pending = self._state()["pending"]
            if pending is None:
                return {"status": "nothing_to_recover"}
            plan_id = self._check_id(pending["plan_id"])
            batch = read_json(self._batch_path(plan_id))
            target_root = self.home / "items" / plan_id
            # An item may have been renamed before its journal entry was saved.
            for item in batch["items"]:
                if item["status"] == "planned" and not os.path.lexists(item["path"]) and (target_root / str(item["index"])).exists():
                    item["status"] = "moved"
            self._rollback_items(batch, target_root)
            batch["status"] = "failed" if pending["op"] == "apply" else "rolled_back"
            atomic_json(self._batch_path(plan_id), batch)
            self._set_pending(None)
            self._audit("recover", "restored", plan_id, op=pending["op"])
            return {"status": "recovered", "plan_id": plan_id, "interrupted_op": pending["op"]}

    # ---------- quarantine ----------
    def list_quarantine(self) -> list[dict]:
        out = []
        for path in sorted((self.home / "batches").glob("*.json")):
            batch = read_json(path)
            out.append({"plan_id": batch["plan_id"], "status": batch["status"], "applied_at": batch["applied_at"],
                        "items": len(batch["items"]), "bytes": sum(i["bytes"] for i in batch["items"]),
                        "paths": [i["path"] for i in batch["items"]][:20]})
        return out

    def purge(self, *, keep_days: int = DEFAULT_KEEP_DAYS, confirm_code: str | None = None) -> dict:
        """Without confirm_code: dry run that returns what would be deleted and a code.
        With the code from that dry run: really delete those batches. Not reversible."""
        with self.locked():
            self._require_idle()
            cutoff = self.clock() - keep_days * 86400
            due = [b for b in self.list_quarantine() if b["status"] == "applied" and b["applied_at"] <= cutoff]
            ids = sorted(b["plan_id"] for b in due)
            code = hashlib.sha256(("purge:" + ",".join(ids)).encode()).hexdigest()[:6]
            if confirm_code is None:
                return {"dry_run": True, "batches": due, "bytes": sum(b["bytes"] for b in due), "confirm_code": code if ids else None}
            if not ids or not secrets.compare_digest(str(confirm_code), code):
                raise TxnError("E_CONFIRM", "run a dry run again; the due list changed")
            items_root = os.path.realpath(self.home / "items")
            for plan_id in ids:
                folder = self.home / "items" / self._check_id(plan_id)
                if os.path.realpath(folder) != os.path.join(items_root, plan_id):
                    raise TxnError("E_UNSAFE_QUARANTINE", str(folder))
                shutil.rmtree(folder)
                batch = read_json(self._batch_path(plan_id))
                batch["status"] = "purged"
                atomic_json(self._batch_path(plan_id), batch)
                self._audit("purge", "deleted", plan_id)
            return {"dry_run": False, "purged": ids, "bytes": sum(b["bytes"] for b in due)}
