"""
Bridge between local Redis (Vault + Inventory) and remote PostgreSQL (Server).

Data flow:
    Mint:   device generates keypair
            → private key → SecureVault (Redis db=0)
            → public key  → CoinInventoryServer (PostgreSQL) via upload_coins()

    Fetch:  Alice needs Bob's keys
            → fetch from CoinInventoryServer (PostgreSQL)
            → cache in SmartInventory (Redis db=1) via store_key()
"""

from uuid import UUID

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.types import CoinUpload, CoinRecord
from AQM_Database.aqm_shared.errors import BudgetExceededError
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer


async def fetch_and_cache(
    server: CoinInventoryServer,
    inventory: SmartInventory,
    contact_id: str,
    target_user_id: UUID,
    requester_id: UUID,
    coin_category: str,
    count: int,
) -> list[CoinRecord]:
    """Fetch coins from the server and store them in local inventory.

    Returns the list of coins that were both fetched AND successfully cached.
    Coins that fail budget enforcement are silently skipped.
    """
    coins = await server.fetch_coins(target_user_id, requester_id, coin_category, count)

    cached = []
    for coin in coins:
        try:
            inventory.store_key(
                contact_id=contact_id,
                key_id=coin.key_id,
                coin_category=coin.coin_category,
                public_key=coin.public_key_blob,
                signature=coin.signature_blob,
            )
            cached.append(coin)
        except BudgetExceededError:
            break

    return cached


async def upload_coins(
    server: CoinInventoryServer,
    user_id: UUID,
    coins: list[CoinUpload],
) -> int:
    """Upload freshly minted public keys to the server.

    Called after minting; the private halves should already be in the vault.
    Returns count of coins actually inserted (duplicates silently skipped).
    """
    return await server.upload_coins(user_id, coins)


async def sync_inventory(
    server: CoinInventoryServer,
    inventory: SmartInventory,
    contact_id: str,
    target_user_id: UUID,
    requester_id: UUID,
) -> dict[str, int]:
    """Top up local inventory for a contact to their budget caps.

    Checks current stock per tier, computes deficit against budget caps,
    and fetches only what's needed from the server.

    Returns dict of {tier: count_fetched}.
    """
    priority = inventory.get_contact_meta(contact_id)
    if priority is None:
        return {"GOLD": 0, "SILVER": 0, "BRONZE": 0}

    caps = config.BUDGET_CAPS[priority.priority]
    summary = inventory.get_inventory(contact_id)
    current = {
        "GOLD": summary.gold_count,
        "SILVER": summary.silver_count,
        "BRONZE": summary.bronze_count,
    }

    fetched_counts = {}
    for tier in ("GOLD", "SILVER", "BRONZE"):
        deficit = caps[tier] - current[tier]
        if deficit <= 0:
            fetched_counts[tier] = 0
            continue

        cached = await fetch_and_cache(
            server, inventory, contact_id,
            target_user_id, requester_id, tier, deficit,
        )
        fetched_counts[tier] = len(cached)

    return fetched_counts
