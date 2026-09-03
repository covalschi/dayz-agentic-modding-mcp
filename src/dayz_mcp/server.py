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
    # tens of seconds AFTER the server reports ready (18-38 s across the boots
    # measured so far), so it is the tool that belongs between a finished boot
    # job and the first world command.
    tools.world_ready, tools.world_state,
    tools.world_spawn, tools.world_teleport, tools.world_set, tools.world_delete,
    tools.world_action, tools.world_exec,
    # The client: its lifecycle, then its eyes, then its hands. client_type is
    # last of the input tools and stands apart on purpose -- it is the ONLY
    # tool in this whole set that takes the foreground away from whoever is at
    # the machine, and its own description says so. Everything above it works
    # with the client sitting in the background, chat included.
    tools.client_start, tools.client_status, tools.client_stop,
    tools.client_shot,
    tools.client_move, tools.client_look, tools.client_press,
    tools.client_trigger,
    tools.client_chat, tools.client_type, tools.client_key,
    tools.client_verdict,
    # The index. Build first, then ask -- and knowledge_status stands between
    # them because it is the one that says whether an answer can be trusted
    # yet. find/show/overrides are ordered the way a question narrows: what
    # exists, what it looks like in full, who changes it.
    tools.knowledge_build, tools.knowledge_status,
    tools.knowledge_find, tools.knowledge_show, tools.knowledge_overrides,
    tools.knowledge_callers, tools.mod_lint, tools.server_signatures,
    tools.world_entities, tools.world_time_set, tools.world_weather_set,
    tools.ui_menu, tools.ui_tree, tools.ui_find, tools.ui_click, tools.ui_text,
    tools.ui_load, tools.ui_unload, tools.ui_preview,
    # The active mod set. It stands after the search tools because it changes
    # what they answer: knowledge_scope declares the subset a server runs, and
    # server_mods proposes one from a live address without applying it. Nothing
    # they filter out is hidden -- an answer outside the set is named with its
    # mod, which is why declaring one is safe to do early.
    tools.knowledge_scope, tools.server_mods,
    # The asset pipeline, in the order a model travels. asset_export comes
    # first and is the OPTIONAL step -- a model already exported to .p3d
    # skips it -- so it stands ahead of the build rather than inside it.
    # asset_check stands between build and convert on purpose: it is the tool
    # that answers for a mod nobody has built here, which is the state a fresh
    # clone is in. asset_convert is last, the one step of the four that
    # touches no model at all.
    tools.asset_export, tools.asset_build, tools.asset_check, tools.asset_convert,
):
    mcp.tool()(_wrap(_fn))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
