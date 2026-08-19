"""MCP entry point: a thin wrapper. All behaviour lives in dayz_mcp.tools."""
from __future__ import annotations

import functools

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from . import DIST_NAME, __version__, tools

mcp = FastMCP(DIST_NAME)

# What `initialize` reports as serverInfo.version. Without this line the low-level
# server falls back to pkg_version("mcp") -- the SDK's OWN version
# (lowlevel/server.py: `self.version if self.version else pkg_version("mcp")`),
# so a client asking what version of this product it is talking to was told
# "1.29.0", confirmed over a real stdio session. That answer is not merely
# wrong once: it would keep reporting the SDK's version through every release
# this project ever makes, and move when the SDK moves.
#
# Assigned after construction because FastMCP 1.29.0 takes no `version`
# argument and never passes one on (server.py: MCPServer(name=..., instructions=
# ..., website_url=..., icons=..., lifespan=...)). `_mcp_server` is the only
# route to it -- there is no public alias -- and `mcp` is pinned to an exact
# version here, with a test that fails if a future SDK moves this.
mcp._mcp_server.version = __version__


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
    tools.bridge_build, tools.bridge_status, tools.bridge_clear,
    # world_ready comes first on purpose: the bridge starts reading commands
    # about 35 seconds AFTER the server reports ready (measured twice on a live
    # stand), so it is the tool that belongs between a finished boot job and the
    # first world command.
    tools.world_ready, tools.world_state,
    tools.world_spawn, tools.world_teleport, tools.world_set, tools.world_delete,
):
    mcp.tool()(_wrap(_fn))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
