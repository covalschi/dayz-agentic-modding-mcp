"""The package's own version, read from its installed metadata.

Sourced from importlib.metadata rather than a literal, because a literal here
is a second copy of the number in pyproject.toml and the two drift the moment
one of them is bumped alone. This is what the MCP server reports as
serverInfo.version -- see server.py, and note what it reported before it
reported this.
"""
from importlib.metadata import PackageNotFoundError, version

DIST_NAME = "dayz-agentic-modding-mcp"

try:
    __version__ = version(DIST_NAME)
except PackageNotFoundError:  # a source checkout that was never installed
    # "unknown" rather than a guess: the same answer the MCP SDK gives for a
    # package it cannot find, and an honest one. A plausible-looking number
    # invented here would be indistinguishable from a real release.
    __version__ = "unknown"

__all__ = ["DIST_NAME", "__version__"]
