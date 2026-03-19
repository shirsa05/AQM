import pytest
import pytest_asyncio
import websockets
from AQM_Database.aqm_network.relay_server import RelayServer


@pytest.fixture
def relay_host():
    return "localhost"


@pytest.fixture
def relay_port():
    return 9876


@pytest_asyncio.fixture
async def relay_server(relay_host, relay_port):
    """Start a RelayServer on a background task, yield it, then shut down."""
    server = RelayServer(relay_host, relay_port)
    ws_server = await websockets.serve(
        server.handle_connection, relay_host, relay_port
    )
    yield server
    ws_server.close()
    await ws_server.wait_closed()


@pytest.fixture
def server_url(relay_host, relay_port):
    return f"ws://{relay_host}:{relay_port}"
