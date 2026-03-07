import time
from typing import Optional

import redis

from AQM_Database.aqm_shared import errors, config
from AQM_Database.aqm_shared.types import ContactMeta, InventoryEntry, InventorySummary


class SmartInventory:
    def __init__(self, client: redis.Redis):
        self.db: redis.Redis = client

    def _meta_key(self, contact_id: str) -> str:
        return f"{config.INV_META_PREFIX}:{contact_id}"

    def _idx_key(self, contact_id: str, coin_category: str) -> str:
        return f"{config.INV_IDX_PREFIX}:{contact_id}:{coin_category}"

    def _inv_key(self, contact_id: str, key_id: str) -> str:
        return f"{config.INV_KEY_PREFIX}:{contact_id}:{key_id}"

    def _validate_priority(self, priority: str) -> None:
        if priority not in config.VALID_PRIORITIES:
            raise errors.InvalidPriorityError(priority)

    def _validate_coin_category(self, coin_category: str) -> None:
        if coin_category not in config.VALID_COIN_CATEGORIES:
            raise errors.InvalidCoinCategoryError(coin_category)

    def _get_priority(self, contact_id: str) -> str:
        val = self.db.hget(self._meta_key(contact_id), "priority")
        if val is None:
            raise errors.ContactNotRegisteredError(contact_id)
        return val.decode()

    def _serialize_entry(self, contact_id: str, key_id: str, coin_category: str,
                         public_key: bytes, signature: bytes) -> dict:
        return {
            "contact_id": contact_id,
            "key_id": key_id,
            "coin_category": coin_category,
            "public_key": public_key,
            "signature": signature,
            "fetched_at": str(int(time.time() * 1000)),
        }

    def _deserialize_entry(self, contact_id: str, key_id: str, data: dict) -> InventoryEntry:
        return InventoryEntry(
            contact_id=data[b"contact_id"].decode(),
            key_id=key_id,
            coin_category=data[b"coin_category"].decode(),
            public_key=data[b"public_key"],
            signature=data[b"signature"],
            fetched_at=int(data[b"fetched_at"]),
        )

    def _pop_from_tier(self, contact_id: str, coin_category: str) -> Optional[InventoryEntry]:
        idx = self._idx_key(contact_id, coin_category)
        result = self.db.zpopmin(idx, count=1)
        if not result:
            return None

        key_id_bytes, score = result[0]
        key_id = key_id_bytes.decode()

        inv_key = self._inv_key(contact_id, key_id)
        data = self.db.hgetall(inv_key)
        if not data:
            return None
        self.db.delete(inv_key)

        return self._deserialize_entry(contact_id, key_id, data)

    def _trim_excess(self, contact_id: str, new_priority: str) -> int:
        new_caps = config.BUDGET_CAPS[new_priority]
        total_evicted = 0

        for tier in ("GOLD", "SILVER", "BRONZE"):
            idx = self._idx_key(contact_id, tier)
            current_count = self.db.zcard(idx)
            cap = new_caps[tier]
            excess = current_count - cap

            if excess <= 0:
                continue

            removed = self.db.zpopmax(idx, count=excess)

            pipe = self.db.pipeline(transaction=False)
            for key_id_bytes, _score in removed:
                pipe.delete(self._inv_key(contact_id, key_id_bytes.decode()))
            pipe.execute()

            total_evicted += len(removed)

        return total_evicted

    def _estimate_entry_bytes(self, coin_category: str) -> int:
        return config.COIN_SIZE_BYTES.get(coin_category, 0) + 128  # overhead

    # ─── Contact Management ───

    def register_contact(self, contact_id: str, priority: str, display_name: str = "") -> bool:
        self._validate_priority(priority)
        meta_key = self._meta_key(contact_id)
        try:
            if self.db.exists(meta_key):
                return False
            self.db.hset(meta_key, mapping={
                "contact_id": contact_id,
                "priority": priority,
                "display_name": display_name,
                "last_msg_at": str(int(time.time() * 1000)),
            })
            return True
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("register_contact")

    def set_contact_priority(self, contact_id: str, priority: str) -> bool:
        self._validate_priority(priority)
        meta_key = self._meta_key(contact_id)

        try:
            old_priority = self._get_priority(contact_id)
            if old_priority == priority:
                return True

            self.db.hset(meta_key, "priority", priority)

            old_rank = ["BESTIE", "MATE", "STRANGER"].index(old_priority)
            new_rank = ["BESTIE", "MATE", "STRANGER"].index(priority)

            if new_rank > old_rank:
                self._trim_excess(contact_id, priority)

            return True
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("set_contact_priority")

    def get_contact_meta(self, contact_id: str) -> Optional[ContactMeta]:
        try:
            data = self.db.hgetall(self._meta_key(contact_id))
            if not data:
                return None

            return ContactMeta(
                contact_id=data[b"contact_id"].decode(),
                priority=data[b"priority"].decode(),
                last_msg_at=int(data[b"last_msg_at"]),
                display_name=data[b"display_name"].decode(),
            )
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("get_contact_meta")

    # ─── Write Operations ───

    def store_key(
        self,
        contact_id: str,
        key_id: str,
        coin_category: str,
        public_key: bytes,
        signature: bytes,
    ) -> bool:
        self._validate_coin_category(coin_category)

        try:
            priority = self._get_priority(contact_id)
            cap = config.BUDGET_CAPS[priority][coin_category]

            if cap == 0:
                raise errors.BudgetExceededError(contact_id, coin_category, 0, 0)

            idx = self._idx_key(contact_id, coin_category)

            for attempt in range(config.INV_OPTIMISTIC_LOCK_RETRIES):
                self.db.watch(idx)
                current_count = self.db.zcard(idx)

                if current_count >= cap:
                    self.db.unwatch()
                    raise errors.BudgetExceededError(contact_id, coin_category, current_count, cap)

                mapping = self._serialize_entry(contact_id, key_id, coin_category, public_key, signature)
                fetched_at = int(mapping["fetched_at"])

                try:
                    pipe = self.db.pipeline(transaction=True)
                    pipe.hset(self._inv_key(contact_id, key_id), mapping=mapping)
                    pipe.zadd(idx, {key_id: fetched_at})
                    pipe.execute()
                    return True
                except redis.WatchError:
                    continue

            raise errors.ConcurrencyError("store_key")
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("store_key")

    # ─── Read / Consume Operations ───

    def select_coin(self, contact_id: str, desired_tier: str) -> Optional[InventoryEntry]:
        self._validate_coin_category(desired_tier)
        try:
            self._get_priority(contact_id)
            tiers_to_try = [desired_tier] + config.TIER_FALLBACK[desired_tier]

            for tier in tiers_to_try:
                entry = self._pop_from_tier(contact_id, tier)
                if entry is not None:
                    self.db.hset(self._meta_key(contact_id), "last_msg_at", str(int(time.time() * 1000)))
                    return entry

            return None
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("select_coin")

    def consume_key(self, contact_id: str, key_id: str) -> bool:
        try:
            inv_key = self._inv_key(contact_id, key_id)
            data = self.db.hget(inv_key, "coin_category")

            if data is None:
                return False

            coin_category = data.decode()
            pipe = self.db.pipeline(transaction=False)
            pipe.delete(inv_key)
            pipe.zrem(self._idx_key(contact_id, coin_category), key_id)
            pipe.execute()
            return True
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("consume_key")

    # ─── Query Operations ───

    def get_inventory(self, contact_id: Optional[str] = None) -> dict | InventorySummary:
        try:
            if contact_id is not None:
                meta = self.get_contact_meta(contact_id)
                if meta is None:
                    raise errors.ContactNotRegisteredError(contact_id)

                gold = self.db.zcard(self._idx_key(contact_id, "GOLD"))
                silver = self.db.zcard(self._idx_key(contact_id, "SILVER"))
                bronze = self.db.zcard(self._idx_key(contact_id, "BRONZE"))

                return InventorySummary(
                    contact_id=contact_id,
                    gold_count=gold,
                    silver_count=silver,
                    bronze_count=bronze,
                    priority=meta.priority,
                )

            result = {}
            cursor = 0
            while True:
                cursor, keys = self.db.scan(cursor=cursor, match=f"{config.INV_META_PREFIX}:*", count=100)
                for meta_key in keys:
                    cid = meta_key.decode().split(":")[-1]
                    result[cid] = self.get_inventory(cid)

                if cursor == 0:
                    break

            return result
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("get_inventory")

    def has_keys_for(self, contact_id: str) -> bool:
        try:
            for tier in ("GOLD", "SILVER", "BRONZE"):
                if self.db.zcard(self._idx_key(contact_id, tier)) > 0:
                    return True
            return False
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("has_keys_for")

    def get_available_tiers(self, contact_id: str) -> list[str]:
        try:
            available = []
            pipe = self.db.pipeline(transaction=False)
            pipe.zcard(self._idx_key(contact_id, "GOLD"))
            pipe.zcard(self._idx_key(contact_id, "SILVER"))
            pipe.zcard(self._idx_key(contact_id, "BRONZE"))
            counts = pipe.execute()

            for tier, count in zip(("GOLD", "SILVER", "BRONZE"), counts):
                if count > 0:
                    available.append(tier)
            return available
        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("get_available_tiers")