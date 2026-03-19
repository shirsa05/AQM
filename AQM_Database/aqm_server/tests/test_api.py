"""Tests for FastAPI endpoints using httpx AsyncClient."""

import base64
import os
import pytest
import pytest_asyncio
from uuid import uuid4

from httpx import AsyncClient, ASGITransport

from AQM_Database.aqm_server.api import app
from AQM_Database.aqm_server import db


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _inject_pool(pool):
    """Inject the test pool into db module so the API uses it."""
    db.pool = pool
    yield


def _b64(n: int) -> str:
    return base64.b64encode(os.urandom(n)).decode()


def _make_upload_body(user_id, coins_spec):
    """coins_spec: list of (category, pk_size, sig_size)"""
    coins = []
    for cat, pk_sz, sig_sz in coins_spec:
        coins.append({
            "key_id": str(uuid4()),
            "coin_category": cat,
            "public_key_b64": _b64(pk_sz),
            "signature_b64": _b64(sig_sz),
        })
    return {"user_id": str(user_id), "coins": coins}


# ── Upload ──


async def test_upload_endpoint():
    bob = uuid4()
    body = _make_upload_body(bob, [
        ("GOLD", 1184, 2420),
        ("SILVER", 1184, 64),
        ("BRONZE", 32, 64),
    ])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/coins/upload", json=body)

    assert resp.status_code == 200
    assert resp.json()["inserted"] == 3


# ── Fetch ──


async def test_fetch_endpoint():
    bob = uuid4()
    alice = uuid4()

    body = _make_upload_body(bob, [("SILVER", 1184, 64)] * 3)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/v1/coins/upload", json=body)

        resp = await client.get("/v1/coins/fetch", params={
            "target_user_id": str(bob),
            "requester_id": str(alice),
            "coin_category": "SILVER",
            "count": 2,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["coins"]) == 2
    # Verify b64 blobs are decodable
    for coin in data["coins"]:
        assert base64.b64decode(coin["public_key_b64"])
        assert base64.b64decode(coin["signature_b64"])
        assert coin["coin_category"] == "SILVER"


# ── Count ──


async def test_count_endpoint():
    bob = uuid4()
    body = _make_upload_body(bob, [
        ("GOLD", 1184, 2420),
        ("GOLD", 1184, 2420),
        ("SILVER", 1184, 64),
    ])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/v1/coins/upload", json=body)

        resp = await client.get("/v1/coins/count", params={
            "user_id": str(bob),
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["gold"] == 2
    assert data["silver"] == 1
    assert data["bronze"] == 0


# ── Health ──


async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db_connected"] is True


# ── Validation ──


async def test_invalid_category_422():
    bob = uuid4()
    body = {
        "user_id": str(bob),
        "coins": [{
            "key_id": str(uuid4()),
            "coin_category": "PLATINUM",
            "public_key_b64": _b64(32),
            "signature_b64": _b64(64),
        }],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/coins/upload", json=body)

    assert resp.status_code == 422


async def test_fetch_invalid_category_422():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/coins/fetch", params={
            "target_user_id": str(uuid4()),
            "requester_id": str(uuid4()),
            "coin_category": "DIAMOND",
            "count": 1,
        })

    assert resp.status_code == 422


async def test_fetch_missing_params_422():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/coins/fetch")

    assert resp.status_code == 422


# ── Admin ──


async def test_purge_stale_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/admin/purge-stale", json={"max_age_days": 30})

    assert resp.status_code == 200
    assert "deleted" in resp.json()


async def test_hard_delete_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/admin/hard-delete", json={"grace_hours": 1})

    assert resp.status_code == 200
    assert "deleted" in resp.json()
