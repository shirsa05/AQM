from uuid import UUID

import asyncpg
import asyncio
from AQM_Database.aqm_shared.types import CoinUpload, CoinRecord, InventoryCount
from AQM_Database.aqm_shared import errors


class CoinInventoryServer:
    pool:asyncpg.Pool

    def __init__(self , p : asyncpg.Pool):
        self.pool = p


    async def upload_coins(self, user_id : UUID, coins : list[CoinUpload]) -> int:
        if not coins:
            return 0
        try:
            async with self.pool.acquire() as conn:
                inserted = 0
                async with conn.transaction():
                    for coin in coins:
                        result = await conn.fetchrow("""
                                                     INSERT INTO coin_inventory
                                                         (user_id, key_id, coin_category, public_key_blob, signature_blob)
                                                     VALUES ($1, $2, $3, $4, $5)
                                                     ON CONFLICT (user_id, key_id) DO NOTHING
                                                     RETURNING record_id
                                                     """,
                                                     user_id,
                                                     coin.key_id,
                                                     coin.coin_category,
                                                     coin.public_key_blob,
                                                     coin.signature_blob,
                                                     )
                        if result:
                            inserted += 1

                return inserted
        except asyncpg.PostgresError as e:
            raise errors.UploadError(f"upload_coins failed: {e}")


    async def fetch_coins(self , target_user_id : UUID ,
                          requester_id: UUID ,
                          coin_category:str ,
                          count : int) -> list[CoinRecord]:

        if coin_category not in  ("GOLD", "SILVER", "BRONZE"):
            raise errors.InvalidCoinCategoryError(f"Invalid coin category: {coin_category}")

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    rws = await conn.fetch("""
                                           WITH claimed AS (
                                               SELECT record_id , key_id , public_key_blob , signature_blob
                                               FROM coin_inventory
                                               WHERE user_id = $1 AND coin_category = $2 AND fetched_by IS NULL ORDER BY
                                                   uploaded_at ASC LIMIT $3 FOR UPDATE SKIP LOCKED)

                                           UPDATE coin_inventory ci
                                           SET fetched_by = $4,
                                               fetched_at = NOW()
                                           FROM claimed
                                           WHERE ci.record_id = claimed.record_id
                                           RETURNING claimed.key_id , claimed.public_key_blob , claimed.signature_blob"""
                                           ,target_user_id,
                                           coin_category,
                                           count,
                                           requester_id,)

                    return [
                        CoinRecord(
                            key_id=row["key_id"],
                            coin_category = coin_category,
                            public_key_blob = row["public_key_blob"],
                            signature_blob = row["signature_blob"],
                        )
                        for row in rws
                    ]

        except  asyncpg.PostgresError as e:
            raise errors.FetchError(f"fetch_coins failed: {e}")

    async def get_inventory_count(self, user_id: UUID) -> InventoryCount:
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT coin_category, COUNT(*) as cnt
                    FROM coin_inventory
                    WHERE user_id = $1
                      AND fetched_by IS NULL
                    GROUP BY coin_category
                    """,
                    user_id,
                )

                # rows might not include tiers with 0 count
                counts = {"GOLD": 0, "SILVER": 0, "BRONZE": 0}
                for row in rows:
                    counts[row["coin_category"]] = row["cnt"]

                return InventoryCount(
                    gold=counts["GOLD"],
                    silver=counts["SILVER"],
                    bronze=counts["BRONZE"],
                )

        except asyncpg.PostgresError as e:
            raise errors.ServerDatabaseError(f"get_inventory_count failed: {e}")


    async def purge_stale(self, max_age_days: int = 30) -> int:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    DELETE FROM coin_inventory
                    WHERE uploaded_at < NOW() - INTERVAL '1 day' * $1
                      AND fetched_by IS NULL
                    """,
                    max_age_days,
                )
                # result is "DELETE N" string
                return int(result.split()[-1])

        except asyncpg.PostgresError as e:
            raise errors.ServerDatabaseError(f"purge_stale failed: {e}")

    async def hard_delete_fetched(self, grace_hours: int = 1) -> int:
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    DELETE FROM coin_inventory
                    WHERE fetched_by IS NOT NULL
                      AND fetched_at < NOW() - INTERVAL '1 hour' * $1
                    """,
                    grace_hours,
                )
                return int(result.split()[-1])

        except asyncpg.PostgresError as e:
            raise errors.ServerDatabaseError(f"hard_delete_fetched failed: {e}")


