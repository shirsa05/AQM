import pytest
from uuid import uuid4

from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_shared.errors import UploadError
from .conftest import make_coins


pytestmark = pytest.mark.asyncio


async def test_upload_single_coin(inventory, bob_id):
    coins = make_coins(1, "GOLD")
    inserted = await inventory.upload_coins(bob_id, coins)
    assert inserted == 1

    count = await inventory.get_inventory_count(bob_id)
    assert count.gold == 1
    assert count.silver == 0
    assert count.bronze == 0


async def test_upload_batch(inventory, bob_id):
    coins = make_coins(5, "SILVER") + make_coins(3, "BRONZE")
    inserted = await inventory.upload_coins(bob_id, coins)
    assert inserted == 8

    count = await inventory.get_inventory_count(bob_id)
    assert count.silver == 5
    assert count.bronze == 3


async def test_upload_duplicate_idempotent(inventory, bob_id):
    coins = make_coins(3, "GOLD")
    first = await inventory.upload_coins(bob_id, coins)
    assert first == 3

    # Upload same coins again — ON CONFLICT DO NOTHING
    second = await inventory.upload_coins(bob_id, coins)
    assert second == 0

    count = await inventory.get_inventory_count(bob_id)
    assert count.gold == 3


async def test_upload_empty_list(inventory, bob_id):
    inserted = await inventory.upload_coins(bob_id, [])
    assert inserted == 0


async def test_upload_mixed_tiers(inventory, bob_id):
    coins = make_coins(2, "GOLD") + make_coins(3, "SILVER") + make_coins(4, "BRONZE")
    inserted = await inventory.upload_coins(bob_id, coins)
    assert inserted == 9

    count = await inventory.get_inventory_count(bob_id)
    assert count.gold == 2
    assert count.silver == 3
    assert count.bronze == 4


async def test_upload_multiple_users(inventory):
    bob = uuid4()
    carol = uuid4()

    await inventory.upload_coins(bob, make_coins(3, "GOLD"))
    await inventory.upload_coins(carol, make_coins(2, "SILVER"))

    bob_count = await inventory.get_inventory_count(bob)
    carol_count = await inventory.get_inventory_count(carol)

    assert bob_count.gold == 3
    assert carol_count.silver == 2
    # Bob has no silver, Carol has no gold
    assert bob_count.silver == 0
    assert carol_count.gold == 0
