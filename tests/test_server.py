"""MCP discovery must describe file mutation to approval-capable clients."""
import asyncio

from mac_doctor_mcp.server import mcp


def test_tool_approval_annotations():
    tools = asyncio.run(mcp.list_tools())
    readonly = {'inspect_memory', 'inspect_storage', 'inspect_hardware', 'plan_cleanup', 'list_quarantine'}
    destructive = {'apply_plan', 'rollback', 'recover', 'purge_quarantine'}
    assert {tool.name for tool in tools} == readonly | destructive
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is (tool.name in readonly)
        assert tool.annotations.destructiveHint is (tool.name in destructive)
        if tool.name in destructive:
            assert '可回滚' in tool.description
