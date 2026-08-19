"""MCP entry point: a thin wrapper. All behaviour lives in dayz_mcp.tools."""
from __future__ import annotations

import functools

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
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        return fn(*args, **kwargs).to_dict()

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
