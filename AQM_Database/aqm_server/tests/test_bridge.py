"""Integration tests: Redis ↔ PostgreSQL bridge."""

import os
import pytest
from uuid import uuid4

import fakeredis

from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.bridge import fetch_and_cache, upload_coins, sync_inventory
from .conftest import make_coins


pytestmark = pytest.mark.asyncio


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def local_inventory(redis_client) -> SmartInventory:
    return SmartInventory(redis_client)


@pytest.fixture
def server(pool) -> CoinInventoryServer:
    return CoinInventoryServer(pool)


# ── upload_coins ──


async def test_upload_coins_from_mint(server):
    bob = uuid4()
    coins = make_coins(5, "SILVER")
    inserted = await upload_coins(server, bob, coins)
    assert inserted == 5

    count = await server.get_inventory_count(bob)
    assert count.silver == 5


# ── fetch_and_cache ──


async def test_fetch_and_cache_stores_in_inventory(server, local_inventory):
    bob = uuid4()
    alice = uuid4()
    contact_id = str(bob)

    # Register contact in local inventory
    local_inventory.register_contact(contact_id, "BESTIE", "Bob")

    # Upload coins to server
    coins = make_coins(3, "SILVER")
    await server.upload_coins(bob, coins)

    # Fetch from server → cache locally
    cached = await fetch_and_cache(
        server, local_inventory, contact_id, bob, alice, "SILVER", 3
    )
    assert len(cached) == 3

    # Verify they're in local inventory
    summary = local_inventory.get_inventory(contact_id)
    assert summary.silver_count == 3

    # Verify they're consumed on server
    server_count = await server.get_inventory_count(bob)
    assert server_count.silver == 0


async def test_fetch_and_cache_respects_budget(server, local_inventory):
    bob = uuid4()
    alice = uuid4()
    contact_id = str(bob)

    local_inventory.register_contact(contact_id, "BESTIE", "Bob")
    # BESTIE SILVER cap = 4

    await server.upload_coins(bob, make_coins(10, "SILVER"))

    # Try to fetch 10 — budget caps at 4
    cached = await fetch_and_cache(
        server, local_inventory, contact_id, bob, alice, "SILVER", 10
    )
    assert len(cached) == 4

    summary = local_inventory.get_inventory(contact_id)
    assert summary.silver_count == 4


async def test_fetch_and_cache_partial_availability(server, local_inventory):
    bob = uuid4()
    alice = uuid4()
    contact_id = str(bob)

    local_inventory.register_contact(contact_id, "BESTIE", "Bob")

    # Only 2 coins on server, request 5
    await server.upload_coins(bob, make_coins(2, "GOLD"))

    cached = await fetch_and_cache(
        server, local_inventory, contact_id, bob, alice, "GOLD", 5
    )
    assert len(cached) == 2

    summary = local_inventory.get_inventory(contact_id)
    assert summary.gold_count == 2


# ── sync_inventory ──


async def test_sync_inventory_tops_up(server, local_inventory):
    bob = uuid4()
    alice = uuid4()
    contact_id = str(bob)

    local_inventory.register_contact(contact_id, "BESTIE", "Bob")
    # BESTIE caps: GOLD=5, SILVER=4, BRONZE=1

    # Upload plenty on server
    await server.upload_coins(bob, make_coins(10, "GOLD"))
    await server.upload_coins(bob, make_coins(10, "SILVER"))
    await server.upload_coins(bob, make_coins(10, "BRONZE"))

    # Pre-populate 2 GOLD locally
    for coin in make_coins(2, "GOLD"):
        local_inventory.store_key(
            contact_id, coin.key_id, coin.coin_category,
            coin.public_key_blob, coin.signature_blob,
        )

    # Sync should fetch: GOLD=3 (5-2), SILVER=4 (4-0), BRONZE=1 (1-0)
    result = await sync_inventory(
        server, local_inventory, contact_id, bob, alice
    )

    assert result["GOLD"] == 3
    assert result["SILVER"] == 4
    assert result["BRONZE"] == 1

    # Verify final counts
    summary = local_inventory.get_inventory(contact_id)
    assert summary.gold_count == 5
    assert summary.silver_count == 4
    assert summary.bronze_count == 1


async def test_sync_inventory_already_full(server, local_inventory):
    bob = uuid4()
    alice = uuid4()
    contact_id = str(bob)

    local_inventory.register_contact(contact_id, "BESTIE", "Bob")

    # Fill to cap locally: GOLD=5
    for coin in make_coins(5, "GOLD"):
        local_inventory.store_key(
            contact_id, coin.key_id, coin.coin_category,
            coin.public_key_blob, coin.signature_blob,
        )

    await server.upload_coins(bob, make_coins(5, "GOLD"))

    result = await sync_inventory(
        server, local_inventory, contact_id, bob, alice
    )

    # Already at cap — nothing fetched for gold
    assert result["GOLD"] == 0


async def test_sync_unregistered_contact(server, local_inventory):
    bob = uuid4()
    alice = uuid4()

    result = await sync_inventory(
        server, local_inventory, "unknown-contact", bob, alice
    )
    assert result == {"GOLD": 0, "SILVER": 0, "BRONZE": 0}
