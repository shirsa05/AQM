import asyncio
import pytest
from uuid import uuid4

from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError, FetchError
from .conftest import make_coins


pytestmark = pytest.mark.asyncio


async def test_fetch_returns_correct_tier(inventory, bob_id, alice_id):
    await inventory.upload_coins(bob_id, make_coins(3, "GOLD"))
    await inventory.upload_coins(bob_id, make_coins(3, "SILVER"))

    results = await inventory.fetch_coins(bob_id, alice_id, "GOLD", 2)
    assert len(results) == 2
    assert all(r.coin_category == "GOLD" for r in results)


async def test_fetch_fifo_order(inventory, bob_id, alice_id, pool):
    """Keys uploaded first should be fetched first."""
    coins = make_coins(5, "SILVER")
    await inventory.upload_coins(bob_id, coins)

    # Fetch 2 — should be the first 2 uploaded (by uploaded_at ASC)
    results = await inventory.fetch_coins(bob_id, alice_id, "SILVER", 2)
    assert len(results) == 2

    fetched_key_ids = {r.key_id for r in results}
    # The first 2 coins by upload order
    expected_key_ids = {coins[0].key_id, coins[1].key_id}
    assert fetched_key_ids == expected_key_ids


async def test_fetch_partial_when_insufficient(inventory, bob_id, alice_id):
    await inventory.upload_coins(bob_id, make_coins(2, "BRONZE"))
    results = await inventory.fetch_coins(bob_id, alice_id, "BRONZE", 10)
    assert len(results) == 2


async def test_fetch_empty_returns_empty_list(inventory, bob_id, alice_id):
    results = await inventory.fetch_coins(bob_id, alice_id, "GOLD", 5)
    assert results == []


async def test_fetch_marks_fetched_by(inventory, bob_id, alice_id, pool):
    await inventory.upload_coins(bob_id, make_coins(1, "SILVER"))
    await inventory.fetch_coins(bob_id, alice_id, "SILVER", 1)

    # Key should no longer be available
    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 0

    # Verify fetched_by is set in the DB
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT fetched_by FROM coin_inventory WHERE user_id = $1", bob_id
        )
        assert row["fetched_by"] == alice_id


async def test_fetch_skips_already_fetched(inventory, bob_id):
    alice = uuid4()
    carol = uuid4()

    await inventory.upload_coins(bob_id, make_coins(3, "SILVER"))

    # Alice takes 2
    r1 = await inventory.fetch_coins(bob_id, alice, "SILVER", 2)
    assert len(r1) == 2

    # Carol gets the remaining 1
    r2 = await inventory.fetch_coins(bob_id, carol, "SILVER", 2)
    assert len(r2) == 1

    # No overlap
    ids1 = {r.key_id for r in r1}
    ids2 = {r.key_id for r in r2}
    assert ids1.isdisjoint(ids2)


async def test_fetch_invalid_category(inventory, bob_id, alice_id):
    with pytest.raises(InvalidCoinCategoryError):
        await inventory.fetch_coins(bob_id, alice_id, "PLATINUM", 1)


async def test_concurrent_fetch_no_duplicates(inventory, bob_id, pool):
    """THE critical test: 20 concurrent fetchers, each gets exactly 1 unique key."""
    n = 20
    await inventory.upload_coins(bob_id, make_coins(n, "SILVER"))

    requesters = [uuid4() for _ in range(n)]

    async def fetch_one(requester):
        return await inventory.fetch_coins(bob_id, requester, "SILVER", 1)

    results = await asyncio.gather(*[fetch_one(r) for r in requesters])

    all_keys = []
    for r in results:
        for coin in r:
            all_keys.append(coin.key_id)

    # Every key returned exactly once — no duplicates
    assert len(all_keys) == n
    assert len(set(all_keys)) == n

    # All keys claimed
    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 0


async def test_concurrent_fetch_different_tiers(inventory, bob_id):
    """Concurrent fetches of different tiers don't interfere."""
    await inventory.upload_coins(bob_id, make_coins(5, "GOLD"))
    await inventory.upload_coins(bob_id, make_coins(5, "SILVER"))

    async def fetch_tier(tier):
        requester = uuid4()
        return await inventory.fetch_coins(bob_id, requester, tier, 5)

    gold_res, silver_res = await asyncio.gather(
        fetch_tier("GOLD"), fetch_tier("SILVER")
    )

    assert len(gold_res) == 5
    assert len(silver_res) == 5
    assert all(r.coin_category == "GOLD" for r in gold_res)
    assert all(r.coin_category == "SILVER" for r in silver_res)
