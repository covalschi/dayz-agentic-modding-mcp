import pytest


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio tests on asyncio -- the only backend this project
    (and the FastMCP server it tests) actually uses."""
    return "asyncio"
