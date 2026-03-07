import time
from typing import Optional

import redis

from AQM_Database.aqm_shared import errors, config
from AQM_Database.aqm_shared.types import VaultEntry, VaultStats


class SecureVault:
    def __init__(self, client: redis.Redis):
        self.db: redis.Redis = client

    def _vault_key(self, key_id: str) -> str:
        return f"{config.VAULT_KEY_PREFIX}:{key_id}"

    def _validate_coin_category(self, coin_category: str) -> None:
        if coin_category not in config.VALID_COIN_CATEGORIES:
            raise errors.InvalidCoinCategoryError(coin_category)

    def _serialize_entry(
        self,
        key_id: str,
        coin_category: str,
        encrypted_blob: bytes,
        encryption_iv: bytes,
        auth_tag: bytes,
        coin_version: str,
    ) -> dict:
        return {
            "key_id": key_id,
            "coin_category": coin_category,
            "encrypted_blob": encrypted_blob,
            "encryption_iv": encryption_iv,
            "auth_tag": auth_tag,
            "coin_version": coin_version,
            "status": "ACTIVE",
            "created_at": str(int(time.time() * 1000)),
        }

    def _deserialize_entry(self, data: dict[bytes, bytes]) -> VaultEntry:
        return VaultEntry(
            key_id=data[b"key_id"].decode(),
            coin_category=data[b"coin_category"].decode(),
            encrypted_blob=data[b"encrypted_blob"],
            encryption_iv=data[b"encryption_iv"],
            auth_tag=data[b"auth_tag"],
            status=data[b"status"].decode(),
            created_at=int(data[b"created_at"]),
            coin_version=data[b"coin_version"].decode(),
        )

    def store_key(
        self,
        key_id: str,
        coin_category: str,
        encrypted_blob: bytes,
        encryption_iv: bytes,
        auth_tag: bytes,
        coin_version: str = "kyber768_v1",
    ) -> bool:
        self._validate_coin_category(coin_category)
        full_key = self._vault_key(key_id)

        try:
            if self.db.exists(full_key):
                raise errors.KeyAlreadyExistsError(key_id)

            mapping = self._serialize_entry(
                key_id, coin_category, encrypted_blob, encryption_iv, auth_tag, coin_version
            )

            pipe = self.db.pipeline(transaction=True)
            pipe.hset(full_key, mapping=mapping)
            pipe.expire(full_key, config.VAULT_KEY_TTL_SECONDS)
            pipe.hincrby(config.VAULT_STATS_KEY, f"active_{coin_category.lower()}", 1)
            pipe.execute()

            return True
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("store_key")

    def burn_key(self, key_id: str) -> bool:
        full_key = self._vault_key(key_id)
        try:
            data = self.db.hmget(full_key, "status", "coin_category")
            status, coin_category = data

            if status is None:
                raise errors.KeyNotFoundError(key_id)
            if status.decode() == "BURNED":
                raise errors.KeyAlreadyBurnedError(key_id)

            coin_cat = coin_category.decode()

            pipe = self.db.pipeline(transaction=True)
            pipe.hset(full_key, "status", "BURNED")
            pipe.expire(full_key, config.VAULT_BURN_GRACE_SECONDS)
            pipe.hincrby(config.VAULT_STATS_KEY, f"active_{coin_cat.lower()}", -1)
            pipe.hincrby(config.VAULT_STATS_KEY, "total_burned", 1)
            pipe.execute()

            return True
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("burn_key")

    def fetch_key(self, key_id: str) -> Optional[VaultEntry]:
        full_key = self._vault_key(key_id)
        try:
            data = self.db.hgetall(full_key)
            if not data:
                return None

            if data.get(b"status", b"").decode() == "BURNED":
                return None

            return self._deserialize_entry(data)
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("fetch_key")

    def exists(self, key_id: str) -> bool:
        try:
            return bool(self.db.exists(self._vault_key(key_id)))
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("exists")

    def count_active(self, coin_category: Optional[str] = None) -> dict[str, int] | int:
        if coin_category is not None:
            self._validate_coin_category(coin_category)

        try:
            if coin_category is not None:
                val = self.db.hget(config.VAULT_STATS_KEY, f"active_{coin_category.lower()}")
                return int(val) if val else 0

            stats = self.db.hgetall(config.VAULT_STATS_KEY)
            return {
                "GOLD": int(stats.get(b"active_gold", 0)),
                "SILVER": int(stats.get(b"active_silver", 0)),
                "BRONZE": int(stats.get(b"active_bronze", 0)),
            }
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("count_active")

    def get_all_active_ids(self, coin_category: Optional[str] = None) -> list[str]:
        if coin_category is not None:
            self._validate_coin_category(coin_category)

        try:
            active_ids: list[str] = []
            cursor = 0
            pattern = f"{config.VAULT_KEY_PREFIX}:*"
            while True:
                cursor, keys = self.db.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    pipe = self.db.pipeline(transaction=False)
                    for key in keys:
                        pipe.hmget(key, "status", "coin_category")
                    results = pipe.execute()

                    for key, (status, category) in zip(keys, results):
                        if status is None:
                            continue
                        if status.decode() != "ACTIVE":
                            continue
                        if coin_category and category.decode() != coin_category:
                            continue

                        raw_key = key.decode()
                        key_id = raw_key[len(config.VAULT_KEY_PREFIX) + 1:]
                        active_ids.append(key_id)

                if cursor == 0:
                    break
            return active_ids
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("get_all_active_ids")

    def purge_expired(self, max_age_days: int = 30) -> int:
        cutoff_ms = int((time.time() - max_age_days * 86400) * 1000)
        purged = 0
        cursor = 0
        pattern = f"{config.VAULT_KEY_PREFIX}:*"
        try:
            while True:
                cursor, keys = self.db.scan(cursor=cursor, match=pattern, count=100)
                for key in keys:
                    raw = self.db.hmget(key, "status", "coin_category", "created_at")
                    status, category, created_at = raw

                    if status is None or status.decode() != "ACTIVE":
                        continue
                    if int(created_at) > cutoff_ms:
                        continue

                    coin_cat = category.decode()
                    pipe = self.db.pipeline(transaction=True)
                    pipe.delete(key)
                    pipe.hincrby(config.VAULT_STATS_KEY, f"active_{coin_cat.lower()}", -1)
                    pipe.hincrby(config.VAULT_STATS_KEY, "total_expired", 1)
                    pipe.execute()
                    purged += 1

                if cursor == 0:
                    break
            return purged
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("purge_expired")

    def get_stats(self) -> VaultStats:
        try:
            stats = self.db.hgetall(config.VAULT_STATS_KEY)
            return VaultStats(
                active_gold=int(stats.get(b"active_gold", 0)),
                active_silver=int(stats.get(b"active_silver", 0)),
                active_bronze=int(stats.get(b"active_bronze", 0)),
                total_burned=int(stats.get(b"total_burned", 0)),
                total_expired=int(stats.get(b"total_expired", 0)),
            )
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError("get_stats")