import os
import time

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.types import GCResult


def _make_pub_key(tier="GOLD"):
    sizes = {"GOLD": 1184, "SILVER": 1184, "BRONZE": 32}
    return os.urandom(sizes[tier])


def _make_sig(tier="GOLD"):
    sizes = {"GOLD": 2420, "SILVER": 64, "BRONZE": 64}
    return os.urandom(sizes[tier])


def _make_contact_inactive(inventory_client, inventory, contact_id):
    """Set last_msg_at to 31 days ago."""
    meta_key = inventory._meta_key(contact_id)
    old_ts = str(int((time.time() - 31 * 86400) * 1000))
    inventory_client.hset(meta_key, "last_msg_at", old_ts)


def test_gc_removes_inactive_contacts(gc, inventory, inventory_client, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.store_key(registered_bestie, "silver_001", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))

    _make_contact_inactive(inventory_client, inventory, registered_bestie)

    result = gc.garbage_collect(inactive_days=30)
    assert isinstance(result, GCResult)
    assert result.contacts_cleaned == 1
    assert result.keys_deleted == 2


def test_gc_keeps_active_contacts(gc, inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    # Contact is recent (just registered), should NOT be cleaned
    result = gc.garbage_collect(inactive_days=30)
    assert result.contacts_cleaned == 0
    assert result.keys_deleted == 0


def test_gc_returns_correct_counts(gc, inventory, inventory_client, registered_bestie):
    for i in range(3):
        inventory.store_key(registered_bestie, f"silver_{i}", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))

    _make_contact_inactive(inventory_client, inventory, registered_bestie)

    result = gc.garbage_collect(inactive_days=30)
    assert result.keys_deleted == 3
    assert result.bytes_freed == 3 * config.COIN_SIZE_BYTES["SILVER"]


def test_gc_downgrades_to_stranger(gc, inventory, inventory_client, registered_bestie):
    _make_contact_inactive(inventory_client, inventory, registered_bestie)

    gc.garbage_collect(inactive_days=30)
    meta = inventory.get_contact_meta(registered_bestie)
    assert meta.priority == "STRANGER"


def test_collect_single_contact(gc, inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))

    result = gc.collect_single_contact(registered_bestie)
    assert result.contacts_cleaned == 1
    assert result.keys_deleted == 1
    assert inventory.has_keys_for(registered_bestie) is False


def test_dry_run_does_not_delete(gc, inventory, inventory_client, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))

    _make_contact_inactive(inventory_client, inventory, registered_bestie)

    result = gc.dry_run(inactive_days=30)
    assert result.contacts_cleaned == 1
    assert result.keys_deleted == 1

    # But data is still there
    assert inventory.has_keys_for(registered_bestie) is True


def test_gc_handles_empty_inventory(gc):
    result = gc.garbage_collect(inactive_days=30)
    assert result.contacts_cleaned == 0
    assert result.keys_deleted == 0
    assert result.bytes_freed == 0
