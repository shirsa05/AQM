import pytest
from uuid import uuid4
from datetime import timedelta

from .conftest import make_coins


pytestmark = pytest.mark.asyncio


async def _insert_with_age(pool, bob_id, coins, age_days):
    """Insert coins then backdate their uploaded_at."""
    async with pool.acquire() as conn:
        for coin in coins:
            await conn.execute(
                """
                INSERT INTO coin_inventory
                    (user_id, key_id, coin_category, public_key_blob, signature_blob, uploaded_at)
                VALUES ($1, $2, $3, $4, $5, NOW() - INTERVAL '1 day' * $6)
                """,
                bob_id,
                coin.key_id,
                coin.coin_category,
                coin.public_key_blob,
                coin.signature_blob,
                age_days,
            )


async def _fetch_and_backdate(pool, inventory, bob_id, coins, hours_ago):
    """Fetch coins then backdate their fetched_at."""
    requester = uuid4()
    await inventory.upload_coins(bob_id, coins)
    await inventory.fetch_coins(bob_id, requester, coins[0].coin_category, len(coins))

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE coin_inventory
            SET fetched_at = NOW() - INTERVAL '1 hour' * $1
            WHERE user_id = $2 AND fetched_by IS NOT NULL
            """,
            hours_ago,
            bob_id,
        )


# ── purge_stale tests ──


async def test_purge_stale_removes_old_unfetched(inventory, bob_id, pool):
    old_coins = make_coins(3, "SILVER")
    await _insert_with_age(pool, bob_id, old_coins, age_days=35)

    deleted = await inventory.purge_stale(max_age_days=30)
    assert deleted == 3

    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 0


async def test_purge_stale_keeps_recent(inventory, bob_id):
    recent_coins = make_coins(3, "SILVER")
    await inventory.upload_coins(bob_id, recent_coins)

    deleted = await inventory.purge_stale(max_age_days=30)
    assert deleted == 0

    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 3


async def test_purge_stale_ignores_fetched(inventory, bob_id, pool, alice_id):
    """Fetched keys should NOT be purged by purge_stale, even if old."""
    old_coins = make_coins(2, "GOLD")
    await _insert_with_age(pool, bob_id, old_coins, age_days=35)

    # Fetch them (marks fetched_by)
    await inventory.fetch_coins(bob_id, alice_id, "GOLD", 2)

    deleted = await inventory.purge_stale(max_age_days=30)
    # These are fetched, not unfetched — purge_stale only targets unfetched
    assert deleted == 0


# ── hard_delete_fetched tests ──


async def test_hard_delete_removes_old_fetched(inventory, bob_id, pool):
    coins = make_coins(3, "BRONZE")
    await _fetch_and_backdate(pool, inventory, bob_id, coins, hours_ago=5)

    deleted = await inventory.hard_delete_fetched(grace_hours=1)
    assert deleted == 3

    # Table is now empty
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM coin_inventory WHERE user_id = $1", bob_id
        )
        assert total == 0


async def test_hard_delete_keeps_recent_fetched(inventory, bob_id, alice_id):
    coins = make_coins(2, "SILVER")
    await inventory.upload_coins(bob_id, coins)
    await inventory.fetch_coins(bob_id, alice_id, "SILVER", 2)

    # Just fetched — within grace period
    deleted = await inventory.hard_delete_fetched(grace_hours=1)
    assert deleted == 0


async def test_full_lifecycle(inventory, bob_id, pool):
    """Upload → fetch → verify claimed → purge stale → hard delete."""
    # 1. Upload
    coins = make_coins(5, "SILVER")
    inserted = await inventory.upload_coins(bob_id, coins)
    assert inserted == 5

    # 2. Fetch 3
    alice = uuid4()
    fetched = await inventory.fetch_coins(bob_id, alice, "SILVER", 3)
    assert len(fetched) == 3

    # 3. Verify count
    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 2

    # 4. Purge stale — recent, so nothing purged
    purged = await inventory.purge_stale(max_age_days=30)
    assert purged == 0

    # 5. Hard delete — recently fetched, so nothing deleted
    deleted = await inventory.hard_delete_fetched(grace_hours=1)
    assert deleted == 0

    # 6. Backdate the fetched rows and hard delete
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE coin_inventory
            SET fetched_at = NOW() - INTERVAL '2 hours'
            WHERE user_id = $1 AND fetched_by IS NOT NULL
            """,
            bob_id,
        )
    deleted = await inventory.hard_delete_fetched(grace_hours=1)
    assert deleted == 3

    # Only 2 unfetched remain
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM coin_inventory WHERE user_id = $1", bob_id
        )
        assert total == 2
