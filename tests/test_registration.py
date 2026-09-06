"""Every tool this server exports is registered, described, and callable with
the parameter names it actually declares.

One sweep, not one test per namespace. The defect these guard against is a
single one -- a lost `functools.wraps` in `server._wrap`, which makes FastMCP
publish an opaque `args/kwargs` schema and leaves the agent unable to call the
tool at all -- and it is not per-namespace: it hits every tool at once. Twelve
hand-written copies across five files each checked their own three or four
names against their own list of expected parameters, which meant a new tool was
covered only if somebody remembered to add it. Read off `tools.__all__`, the
next tool is covered on the day it is written.
"""
from __future__ import annotations

import inspect

import pytest

from dayz_mcp import server as mcp_server
from dayz_mcp import tools


@pytest.mark.anyio
async def test_every_exported_tool_is_registered_described_and_named(anyio_backend):
    listed = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}

    for name in tools.__all__:
        assert name in listed, f"{name} is exported but not registered"
        assert (listed[name].description or "").strip(), f"{name} has no description"

        declared = [
            p.name for p in inspect.signature(getattr(tools, name)).parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        published = set(listed[name].inputSchema.get("properties", {}))
        assert published == set(declared), (
            f"{name}: the schema publishes {sorted(published)} but the function takes "
            f"{sorted(declared)} -- the wrapper lost the signature"
        )


@pytest.mark.anyio
async def test_nothing_is_registered_that_the_package_does_not_export(anyio_backend):
    """The other direction: a tool reachable over MCP but absent from
    `tools.__all__` is one nothing in this suite would ever look at."""
    listed = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert listed == set(tools.__all__)
