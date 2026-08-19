"""MCP entry point: a thin wrapper. All behaviour lives in dayz_mcp.tools."""
from __future__ import annotations

import functools

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("dayz-agentic-modding-mcp")


def _wrap(fn):
    # functools.wraps sets __wrapped__, which inspect.signature() (used by FastMCP
    # to build each tool's parameter schema) follows by default. A bare
    # `def inner(*args, **kwargs)` has none of the original parameter names or
    # types, so FastMCP would expose a tool that only accepts opaque "args"/
    # "kwargs" fields instead of e.g. `path` or `since` -- confirmed by
    # inspecting the registered inputSchema before adding this.
    #
    # `inner` is async and hands `fn` to a worker thread via anyio.to_thread:
    # FastMCP 1.29.0 calls synchronous tools inline on the server's event loop
    # (func_metadata.py:93-96 -- anyio.to_thread there is used for resources
    # only, not tools), so a call that runs for a while -- job_wait with a large
    # timeout, or server_status's pulse -- would otherwise stall the entire
    # server, including protocol ping and cancellation, not just that one call.
    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        return result.to_dict()

    return inner


for _fn in (
    tools.project_open, tools.project_status, tools.mod_build,
    tools.server_start, tools.server_status, tools.server_stop, tools.client_compile_check,
    tools.log_verdict, tools.log_tail,
    tools.job_status, tools.job_wait, tools.job_artifacts,
):
    mcp.tool()(_wrap(_fn))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
