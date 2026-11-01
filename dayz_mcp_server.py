"""Start the dayz MCP server: the file the Claude Code plugin runs.

`uv run` has installed the package by the time this file runs, so it only hands
over to the server's entry point -- the same one the `dayz-mcp` command calls.
A plain file under the plugin root is what the plugin directory's check can read.
"""
from dayz_mcp.server import main

main()
