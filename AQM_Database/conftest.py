import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop shared by all async fixtures and tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
