"""Replay a real MCP cleanup/rollback on disposable synthetic files only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def digest(folder):
    return hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted(folder.iterdir()))).hexdigest()


async def main():
    with tempfile.TemporaryDirectory(prefix="doctor-demo-") as scratch:
        root = Path(scratch).resolve()
        cache = root / "cache"
        item = cache / "sample"
        item.mkdir(parents=True)
        for i in range(3):
            (item / f"chunk-{i}.txt").write_text(f"synthetic cache chunk {i}\n")
        before = digest(item)
        config = root / "config.json"
        config.write_text(json.dumps({"allow_roots": [str(cache)], "artifact_roots": []}))
        params = StdioServerParameters(command=sys.executable, args=["-m", "mac_doctor_mcp.server"], env={**os.environ, "MAC_DOCTOR_HOME": str(root / "state"), "MAC_DOCTOR_CONFIG": str(config)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError
                    data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    assert data["ok"], data
                    return data
                plan = await call("plan_cleanup", {"items": [{"path": str(item), "reason": "Disposable demo fixture", "risk": "low"}]})
                # This confirmation is solely for files created above, never a user cache.
                applied = await call("apply_plan", {"plan_id": plan["plan_id"], "confirm_code": plan["confirm_code"]})
                isolated = not item.exists()
                restored = await call("rollback", {"plan_id": plan["plan_id"]})
                print(json.dumps({"tool_count": len(tools.tools), "planned_items": len(plan["items"]), "moved_items": applied["moved"], "quarantined": isolated, "restored_items": restored["restored"], "restored_files": len(list(item.iterdir())), "content_sha_matches": before == digest(item)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
