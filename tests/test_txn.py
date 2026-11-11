import hashlib
import os
from pathlib import Path

import pytest

from mac_doctor_mcp import txn as txn_mod
from mac_doctor_mcp.policy import Policy
from mac_doctor_mcp.txn import Quarantine, TxnError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    cache = home / "Library/Caches"
    projects = home / "项目"
    for d in (cache / "app", projects / "web/node_modules/pkg", projects / "web/src"):
        d.mkdir(parents=True)
    (cache / "app/blob.bin").write_bytes(b"x" * 5000)
    (cache / "big.log").write_bytes(b"log" * 1000)
    (projects / "web/node_modules/pkg/index.js").write_text("module.exports=1")
    (projects / "web/src/main.ts").write_text("keep me")
    clock = Clock()
    q = Quarantine(home / ".mac-doctor", Policy.default(home), clock=clock)
    return {"home": home, "cache": cache, "projects": projects, "q": q, "clock": clock}


def tree_digest(path: Path) -> str:
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(path)):
        dirs.sort()
        for name in sorted(files):
            p = Path(root, name)
            h.update(str(p.relative_to(path)).encode() + p.read_bytes())
    return h.hexdigest()


def items(*paths):
    return [{"path": str(p), "reason": "test", "risk": "low"} for p in paths]


def codes(plan):
    return {r["path"]: r["code"] for r in plan["rejected"]}


def test_plan_accepts_allowed_and_rejects_the_rest(env):
    home, cache, projects, q = env["home"], env["cache"], env["projects"], env["q"]
    (home / ".ssh").mkdir()
    (home / ".ssh/id").write_text("secret")
    os.symlink(cache / "app", cache / "link")
    plan = q.plan(items(
        cache / "app", projects / "web/node_modules",
        projects / "web/src", home / ".ssh/id", cache / "link",
        "relative/path", str(cache) + "/../Caches/app", cache / "missing", cache,
    ))
    assert [i["path"] for i in plan["items"]] == [str(cache / "app"), str(projects / "web/node_modules")]
    c = codes(plan)
    assert c[str(projects / "web/src")] == "E_NOT_ARTIFACT"
    assert c[str(home / ".ssh/id")] == "E_DENIED_PATH"
    assert c[str(cache / "link")] == "E_SYMLINK_IN_PATH"
    assert c["relative/path"] == "E_ABSOLUTE_PATH_REQUIRED"
    assert c[str(cache) + "/../Caches/app"] == "E_NORMALIZED_PATH_REQUIRED"
    assert c[str(cache / "missing")] == "E_MISSING"
    assert c[str(cache)] == "E_OUTSIDE_ALLOWED_ROOTS"  # the root itself is never an item
    assert plan["total_bytes"] > 5000 and len(plan["confirm_code"]) == 6


def test_plan_rejects_overlap_and_denied_content_inside(env):
    cache, projects, q = env["cache"], env["projects"], env["q"]
    (projects / "web/node_modules/pkg/.git").mkdir()
    plan = q.plan(items(cache / "app", cache / "app/blob.bin", projects / "web/node_modules"))
    c = codes(plan)
    assert c[str(cache / "app/blob.bin")] == "E_OVERLAPPING_ITEMS"
    assert c[str(projects / "web/node_modules")] == "E_DENIED_INSIDE"


def test_state_home_under_allowed_root_is_rejected(tmp_path):
    home = tmp_path / "home"
    (home / "Library/Caches").mkdir(parents=True)
    q = Quarantine(home / "Library/Caches/qh", Policy.default(home))
    plan = q.plan(items(home / "Library/Caches/qh"))
    assert codes(plan)[str(home / "Library/Caches/qh")] == "E_STATE_HOME"


def test_wrong_code_moves_nothing(env):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "app"))
    with pytest.raises(TxnError) as e:
        q.apply(plan["plan_id"], "000000" if plan["confirm_code"] != "000000" else "111111")
    assert e.value.code == "E_CONFIRM"
    assert (cache / "app/blob.bin").exists()


def test_apply_then_rollback_restores_same_bytes_and_inode(env):
    cache, projects, q = env["cache"], env["projects"], env["q"]
    nm = projects / "web/node_modules"
    before = {p: (tree_digest(p) if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino)
              for p in (cache / "app", cache / "big.log", nm)}
    plan = q.plan(items(cache / "app", cache / "big.log", nm))
    out = q.apply(plan["plan_id"], plan["confirm_code"])
    assert out["status"] == "applied" and out["moved"] == 3
    assert not (cache / "app").exists() and not nm.exists()
    assert (projects / "web/src/main.ts").read_text() == "keep me"
    with pytest.raises(TxnError) as e:
        q.apply(plan["plan_id"], plan["confirm_code"])
    assert e.value.code == "E_PLAN_ALREADY_USED"
    assert q.rollback(plan["plan_id"])["status"] == "rolled_back"
    after = {p: (tree_digest(p) if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino)
             for p in before}
    assert after == before


def test_item_changed_after_plan_is_refused(env):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "big.log", cache / "app"))
    (cache / "big.log").write_bytes(b"new content")
    with pytest.raises(TxnError) as e:
        q.apply(plan["plan_id"], plan["confirm_code"])
    assert e.value.code == "E_ITEM_CHANGED"
    assert (cache / "app").exists()


def test_plan_expires(env):
    cache, q, clock = env["cache"], env["q"], env["clock"]
    plan = q.plan(items(cache / "app"))
    clock.now += txn_mod.PLAN_TTL_S + 1
    with pytest.raises(TxnError) as e:
        q.apply(plan["plan_id"], plan["confirm_code"])
    assert e.value.code == "E_PLAN_EXPIRED"


def test_failure_midway_rolls_back_moved_items(env, monkeypatch):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "app", cache / "big.log"))
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk hiccup")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(txn_mod.os, "rename", flaky)
    with pytest.raises(TxnError) as e:
        q.apply(plan["plan_id"], plan["confirm_code"])
    assert e.value.code == "E_APPLY_ROLLED_BACK"
    assert (cache / "app/blob.bin").exists() and (cache / "big.log").exists()
    assert q.list_quarantine()[0]["status"] == "failed"


def test_crash_midway_is_recovered(env, monkeypatch):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "app", cache / "big.log"))
    real = os.rename
    calls = {"n": 0}

    def crash(src, dst, *a, **k):
        calls["n"] += 1
        real(src, dst, *a, **k)
        if calls["n"] == 2:  # moved, but the process dies before the journal is saved
            raise KeyboardInterrupt

    monkeypatch.setattr(txn_mod.os, "rename", crash)
    with pytest.raises(KeyboardInterrupt):
        q.apply(plan["plan_id"], plan["confirm_code"])
    monkeypatch.setattr(txn_mod.os, "rename", real)
    with pytest.raises(TxnError) as e:
        q.plan(items(cache / "app")) and q.apply("0" * 32, "x")
    assert e.value.code == "E_RECOVERY_REQUIRED"
    out = q.recover()
    assert out["status"] == "recovered" and out["interrupted_op"] == "apply"
    assert (cache / "app/blob.bin").exists() and (cache / "big.log").exists()
    assert q.recover()["status"] == "nothing_to_recover"


def test_rollback_refuses_to_overwrite_then_recover(env):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "app", cache / "big.log"))
    q.apply(plan["plan_id"], plan["confirm_code"])
    (cache / "app").mkdir()  # something recreated the folder
    with pytest.raises(TxnError) as e:
        q.rollback(plan["plan_id"])
    assert e.value.code == "E_RECOVERY_REQUIRED"
    assert (cache / "big.log").exists()  # restored before hitting the conflict
    (cache / "app").rmdir()
    assert q.recover()["interrupted_op"] == "rollback"
    assert (cache / "app/blob.bin").exists()


def test_purge_needs_age_and_dry_run_code(env):
    cache, q, clock = env["cache"], env["q"], env["clock"]
    plan = q.plan(items(cache / "app"))
    q.apply(plan["plan_id"], plan["confirm_code"])
    dry = q.purge(keep_days=7)
    assert dry["batches"] == [] and dry["confirm_code"] is None
    clock.now += 7 * 86400
    dry = q.purge(keep_days=7)
    assert [b["plan_id"] for b in dry["batches"]] == [plan["plan_id"]]
    with pytest.raises(TxnError):
        q.purge(keep_days=7, confirm_code="nope")
    out = q.purge(keep_days=7, confirm_code=dry["confirm_code"])
    assert out["purged"] == [plan["plan_id"]]
    assert not (q.home / "items" / plan["plan_id"]).exists()
    with pytest.raises(TxnError) as e:
        q.rollback(plan["plan_id"])
    assert e.value.code == "E_NOT_ROLLBACKABLE"


def test_audit_log_written(env):
    cache, q = env["cache"], env["q"]
    plan = q.plan(items(cache / "app"))
    q.apply(plan["plan_id"], plan["confirm_code"])
    ops = [line for line in (q.home / "audit.jsonl").read_text().splitlines()]
    assert any('"operation":"plan"' in l for l in ops) and any('"outcome":"applied"' in l for l in ops)

@pytest.mark.parametrize('name', ['knowledge', 'knowledge-notes', 'notes', 'vault'])
def test_knowledge_directories_remain_protected(env, name):
    target = env['cache'] / name
    target.mkdir()
    plan = env['q'].plan(items(target))
    assert plan['items'] == []
    assert plan['rejected'][0]['code'] == 'E_DENIED_PATH'
