import asyncio
import pytest
import websockets
from AQM_Database.aqm_network.protocol import frame_message, parse_message
from AQM_Database.aqm_network.client import Client
from AQM_Database.aqm_network.relay_server import RelayServer

pytestmark = pytest.mark.asyncio


# ── Integration: auth + routing over real WebSocket ──


class TestAuthHandshake:
    async def test_auth_registers_client(self, relay_server, server_url):
        async with websockets.connect(server_url) as ws:
            await ws.send(frame_message("AUTH", {"user_id": "alice"}))
            await asyncio.sleep(0.05)
            assert "alice" in relay_server.connected_clients

    async def test_auth_bad_type_returns_error(self, relay_server, server_url):
        async with websockets.connect(server_url) as ws:
            await ws.send(frame_message("PARCEL", {"user_id": "alice"}))
            reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg_type, payload = parse_message(reply)
            assert msg_type == "ERROR"
            assert "auth" in payload["reason"].lower()

    async def test_auth_missing_user_id_returns_error(self, relay_server, server_url):
        async with websockets.connect(server_url) as ws:
            await ws.send(frame_message("AUTH", {"not_user": "alice"}))
            reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg_type, _ = parse_message(reply)
            assert msg_type == "ERROR"

    async def test_disconnect_removes_client(self, relay_server, server_url):
        ws = await websockets.connect(server_url)
        await ws.send(frame_message("AUTH", {"user_id": "charlie"}))
        await asyncio.sleep(0.05)
        assert "charlie" in relay_server.connected_clients
        await ws.close()
        await asyncio.sleep(0.05)
        assert "charlie" not in relay_server.connected_clients


class TestMessageRouting:
    async def test_parcel_forwarded_to_online_recipient(self, relay_server, server_url):
        # connect alice and bob
        ws_alice = await websockets.connect(server_url)
        ws_bob = await websockets.connect(server_url)
        await ws_alice.send(frame_message("AUTH", {"user_id": "alice"}))
        await ws_bob.send(frame_message("AUTH", {"user_id": "bob"}))
        await asyncio.sleep(0.05)

        # alice sends to bob
        parcel = frame_message("PARCEL", {
            "sender_id": "alice",
            "recipient_id": "bob",
            "data": "encrypted_hello",
        })
        await ws_alice.send(parcel)

        # bob receives it
        raw = await asyncio.wait_for(ws_bob.recv(), timeout=2.0)
        msg_type, payload = parse_message(raw)
        assert msg_type == "PARCEL"
        assert payload["data"] == "encrypted_hello"
        assert payload["sender_id"] == "alice"

        await ws_alice.close()
        await ws_bob.close()

    async def test_parcel_stored_for_offline_recipient(self, relay_server, server_url):
        ws_alice = await websockets.connect(server_url)
        await ws_alice.send(frame_message("AUTH", {"user_id": "alice"}))
        await asyncio.sleep(0.05)

        # alice sends to dave who is NOT connected
        parcel = frame_message("PARCEL", {
            "sender_id": "alice",
            "recipient_id": "dave",
            "data": "for_dave",
        })
        await ws_alice.send(parcel)
        await asyncio.sleep(0.05)

        assert "dave" in relay_server.mailbox
        assert len(relay_server.mailbox["dave"]) == 1

        await ws_alice.close()

    async def test_pending_delivered_on_connect(self, relay_server, server_url):
        # pre-store a parcel for eve
        stored = frame_message("PARCEL", {
            "sender_id": "alice",
            "recipient_id": "eve",
            "data": "waiting_msg",
        })
        relay_server.store_parcel("eve", stored)

        # eve connects
        ws_eve = await websockets.connect(server_url)
        await ws_eve.send(frame_message("AUTH", {"user_id": "eve"}))

        # should receive the pending parcel
        raw = await asyncio.wait_for(ws_eve.recv(), timeout=2.0)
        msg_type, payload = parse_message(raw)
        assert msg_type == "PARCEL"
        assert payload["data"] == "waiting_msg"

        # mailbox should be drained
        assert "eve" not in relay_server.mailbox

        await ws_eve.close()


# ── Client tests ──


class TestClient:
    async def test_connect_and_disconnect(self, relay_server, server_url):
        client = Client(server_url, "frank")
        await client.connect()
        await asyncio.sleep(0.05)
        assert "frank" in relay_server.connected_clients
        await client.disconnect()
        await asyncio.sleep(0.05)
        assert "frank" not in relay_server.connected_clients

    async def test_send_parcel_before_connect_raises(self):
        client = Client("ws://localhost:1", "nobody")
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.send_parcel("bob", b"data")

    async def test_client_sends_and_receives(self, relay_server, server_url):
        received = []

        async def handler(payload):
            received.append(payload)

        client_a = Client(server_url, "alice")
        client_b = Client(server_url, "bob")
        client_b.on_message(handler)

        await client_a.connect()
        await client_b.connect()
        await asyncio.sleep(0.05)

        await client_a.send_parcel("bob", b"secret_data")
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0]["sender_id"] == "alice"

        await client_a.disconnect()
        await client_b.disconnect()

    async def test_on_message_is_sync_setter(self, relay_server, server_url):
        client = Client(server_url, "test")
        callback = lambda p: None
        client.on_message(callback)
        assert client._on_message is callback
