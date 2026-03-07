import time

import redis

from AQM_Database.aqm_shared import errors, config
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_shared.types import GCResult


class GarbageCollector:
    def __init__(self, inventory: SmartInventory, client: redis.Redis):
        self.inventory = inventory
        self.db = client

    def _is_inactive(self, last_msg_at: int, inactive_days: int) -> bool:
        cutoff_ms = int((time.time() - inactive_days * 86400) * 1000)
        return last_msg_at < cutoff_ms

    def _delete_all_keys_for_contact(self, contact_id: str) -> int:
        deleted = 0

        for tier in ("GOLD", "SILVER", "BRONZE"):
            idx_key = self.inventory._idx_key(contact_id, tier)
            key_ids = self.db.zrange(idx_key, 0, -1)

            if not key_ids:
                continue

            pipe = self.db.pipeline(transaction=False)
            for key_id_bytes in key_ids:
                pipe.delete(self.inventory._inv_key(contact_id, key_id_bytes.decode()))
            pipe.delete(idx_key)
            pipe.execute()

            deleted += len(key_ids)

        return deleted

    def garbage_collect(self, inactive_days: int = 30) -> GCResult:
        contacts_cleaned = 0
        keys_deleted = 0
        bytes_freed = 0

        try:
            cursor = 0
            while True:
                cursor, meta_keys = self.db.scan(cursor=cursor, match=f"{config.INV_META_PREFIX}:*", count=100)

                for meta_key in meta_keys:
                    contact_id = meta_key.decode().split(":")[-1]
                    meta = self.inventory.get_contact_meta(contact_id)

                    if meta is None:
                        continue

                    if not self._is_inactive(meta.last_msg_at, inactive_days):
                        continue

                    summary = self.inventory.get_inventory(contact_id)
                    bytes_freed += summary.gold_count * config.COIN_SIZE_BYTES["GOLD"]
                    bytes_freed += summary.silver_count * config.COIN_SIZE_BYTES["SILVER"]
                    bytes_freed += summary.bronze_count * config.COIN_SIZE_BYTES["BRONZE"]

                    count = self._delete_all_keys_for_contact(contact_id)
                    keys_deleted += count
                    self.inventory.set_contact_priority(contact_id, "STRANGER")
                    contacts_cleaned += 1

                if cursor == 0:
                    break

            return GCResult(
                contacts_cleaned=contacts_cleaned,
                keys_deleted=keys_deleted,
                bytes_freed=bytes_freed,
            )

        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("garbage_collect")

    def collect_single_contact(self, contact_id: str) -> GCResult:
        try:
            meta = self.inventory.get_contact_meta(contact_id)
            if meta is None:
                raise errors.ContactNotRegisteredError(contact_id)

            summary = self.inventory.get_inventory(contact_id)
            bytes_freed = (
                    summary.gold_count * config.COIN_SIZE_BYTES["GOLD"]
                    + summary.silver_count * config.COIN_SIZE_BYTES["SILVER"]
                    + summary.bronze_count * config.COIN_SIZE_BYTES["BRONZE"]
            )

            keys_deleted = self._delete_all_keys_for_contact(contact_id)
            self.inventory.set_contact_priority(contact_id, "STRANGER")

            return GCResult(
                contacts_cleaned=1,
                keys_deleted=keys_deleted,
                bytes_freed=bytes_freed,
            )

        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("collect_single_contact")

    def dry_run(self, inactive_days: int = 30) -> GCResult:
        try:
            contacts_cleaned = 0
            keys_deleted = 0
            bytes_freed = 0
            cursor = 0

            while True:
                cursor, meta_keys = self.db.scan(cursor=cursor, match=f"{config.INV_META_PREFIX}:*", count=100)

                for meta_key in meta_keys:
                    contact_id = meta_key.decode().split(":")[-1]
                    meta = self.inventory.get_contact_meta(contact_id)

                    if meta is None:
                        continue
                    if not self._is_inactive(meta.last_msg_at, inactive_days):
                        continue

                    summary = self.inventory.get_inventory(contact_id)
                    total_keys = summary.gold_count + summary.silver_count + summary.bronze_count
                    keys_deleted += total_keys
                    bytes_freed += (
                            summary.gold_count * config.COIN_SIZE_BYTES["GOLD"]
                            + summary.silver_count * config.COIN_SIZE_BYTES["SILVER"]
                            + summary.bronze_count * config.COIN_SIZE_BYTES["BRONZE"]
                    )
                    contacts_cleaned += 1

                if cursor == 0:
                    break

            return GCResult(
                contacts_cleaned=contacts_cleaned,
                keys_deleted=keys_deleted,
                bytes_freed=bytes_freed,
            )

        except redis.exceptions.ConnectionError:
            raise errors.InventoryUnavailableError("dry_run")