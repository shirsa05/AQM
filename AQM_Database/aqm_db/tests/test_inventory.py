import os
import time

import pytest

from AQM_Database.aqm_shared import errors
from AQM_Database.aqm_shared.types import InventoryEntry, InventorySummary, ContactMeta


def _make_pub_key(tier="GOLD"):
    sizes = {"GOLD": 1184, "SILVER": 1184, "BRONZE": 32}
    return os.urandom(sizes[tier])


def _make_sig(tier="GOLD"):
    sizes = {"GOLD": 2420, "SILVER": 64, "BRONZE": 64}
    return os.urandom(sizes[tier])


# ── Contact Management ──

def test_register_contact_success(inventory):
    assert inventory.register_contact("alice", "BESTIE", "Alice") is True


def test_register_contact_duplicate_is_idempotent(inventory):
    inventory.register_contact("alice", "BESTIE", "Alice")
    assert inventory.register_contact("alice", "BESTIE", "Alice") is False


def test_register_contact_invalid_priority_raises(inventory):
    with pytest.raises(errors.InvalidPriorityError):
        inventory.register_contact("alice", "ENEMY")


def test_get_contact_meta(inventory, registered_bestie):
    meta = inventory.get_contact_meta(registered_bestie)
    assert isinstance(meta, ContactMeta)
    assert meta.priority == "BESTIE"
    assert meta.contact_id == registered_bestie


def test_get_contact_meta_nonexistent_returns_none(inventory):
    assert inventory.get_contact_meta("nonexistent") is None


def test_set_priority_upgrade(inventory, registered_mate):
    assert inventory.set_contact_priority(registered_mate, "BESTIE") is True
    meta = inventory.get_contact_meta(registered_mate)
    assert meta.priority == "BESTIE"


def test_set_priority_downgrade_trims_excess(inventory, registered_bestie):
    # Fill 5 Gold keys for a Bestie
    for i in range(5):
        inventory.store_key(registered_bestie, f"gold_{i}", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))

    # Downgrade to MATE (Gold cap=0) → should trim all Gold
    inventory.set_contact_priority(registered_bestie, "MATE")
    summary = inventory.get_inventory(registered_bestie)
    assert summary.gold_count == 0


def test_set_priority_unregistered_raises(inventory):
    with pytest.raises(errors.ContactNotRegisteredError):
        inventory.set_contact_priority("ghost", "BESTIE")


# ── Store ──

def test_store_key_success(inventory, registered_bestie):
    result = inventory.store_key(
        registered_bestie, "key_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD")
    )
    assert result is True


def test_store_key_budget_exceeded_raises(inventory, registered_bestie):
    # Fill 5 Gold keys (Bestie cap)
    for i in range(5):
        inventory.store_key(registered_bestie, f"gold_{i}", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))

    with pytest.raises(errors.BudgetExceededError):
        inventory.store_key(registered_bestie, "gold_extra", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))


def test_store_key_mate_cannot_store_gold(inventory, registered_mate):
    with pytest.raises(errors.BudgetExceededError):
        inventory.store_key(registered_mate, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))


def test_store_key_stranger_can_store_bronze(inventory):
    """STRANGER budget: 0G / 0S / 5B — BRONZE allowed, GOLD rejected."""
    inventory.register_contact("stranger_001", "STRANGER", "Stranger")
    assert inventory.store_key("stranger_001", "key_001", "BRONZE", _make_pub_key("BRONZE"), _make_sig("BRONZE"))
    with pytest.raises(errors.BudgetExceededError):
        inventory.store_key("stranger_001", "key_002", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))


def test_store_key_unregistered_contact_raises(inventory):
    with pytest.raises(errors.ContactNotRegisteredError):
        inventory.store_key("ghost", "key_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))


def test_store_key_updates_sorted_set_index(inventory, inventory_client, registered_bestie):
    inventory.store_key(registered_bestie, "key_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    idx = inventory._idx_key(registered_bestie, "GOLD")
    assert inventory_client.zcard(idx) == 1


# ── Select Coin ──

def test_select_coin_returns_correct_tier(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    entry = inventory.select_coin(registered_bestie, "GOLD")
    assert entry is not None
    assert isinstance(entry, InventoryEntry)
    assert entry.coin_category == "GOLD"


def test_select_coin_fifo_order(inventory, registered_bestie):
    # Store 3 Silver keys with small time gaps
    for i in range(3):
        inventory.store_key(registered_bestie, f"silver_{i}", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))
        time.sleep(0.01)

    # Select should return oldest first
    entry = inventory.select_coin(registered_bestie, "SILVER")
    assert entry.key_id == "silver_0"


def test_select_coin_fallback_gold_to_silver(inventory, registered_bestie):
    # Only Silver available, request Gold → should fallback
    inventory.store_key(registered_bestie, "silver_001", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))
    entry = inventory.select_coin(registered_bestie, "GOLD")
    assert entry is not None
    assert entry.coin_category == "SILVER"


def test_select_coin_fallback_silver_to_bronze(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "bronze_001", "BRONZE", _make_pub_key("BRONZE"), _make_sig("BRONZE"))
    entry = inventory.select_coin(registered_bestie, "SILVER")
    assert entry is not None
    assert entry.coin_category == "BRONZE"


def test_select_coin_no_upward_fallback(inventory, registered_bestie):
    # Only Gold available, request Bronze → should NOT fallback up
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    entry = inventory.select_coin(registered_bestie, "BRONZE")
    assert entry is None


def test_select_coin_empty_returns_none(inventory, registered_bestie):
    entry = inventory.select_coin(registered_bestie, "GOLD")
    assert entry is None


def test_select_coin_removes_from_inventory(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.select_coin(registered_bestie, "GOLD")
    # Should be gone now
    summary = inventory.get_inventory(registered_bestie)
    assert summary.gold_count == 0


def test_select_coin_updates_last_msg_at(inventory, inventory_client, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    time.sleep(0.05)
    before = int(time.time() * 1000)
    inventory.select_coin(registered_bestie, "GOLD")
    meta = inventory.get_contact_meta(registered_bestie)
    assert meta.last_msg_at >= before - 100  # small tolerance


# ── Consume ──

def test_consume_key_success(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    assert inventory.consume_key(registered_bestie, "gold_001") is True
    summary = inventory.get_inventory(registered_bestie)
    assert summary.gold_count == 0


def test_consume_nonexistent_returns_false(inventory, registered_bestie):
    assert inventory.consume_key(registered_bestie, "nonexistent") is False


# ── Query ──

def test_get_inventory_single_contact(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.store_key(registered_bestie, "silver_001", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))

    summary = inventory.get_inventory(registered_bestie)
    assert isinstance(summary, InventorySummary)
    assert summary.gold_count == 1
    assert summary.silver_count == 1
    assert summary.bronze_count == 0
    assert summary.priority == "BESTIE"


def test_get_inventory_all_contacts(inventory, registered_bestie, registered_mate):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.store_key(registered_mate, "silver_001", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))

    all_inv = inventory.get_inventory()
    assert isinstance(all_inv, dict)
    assert registered_bestie in all_inv
    assert registered_mate in all_inv


def test_has_keys_for_true(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    assert inventory.has_keys_for(registered_bestie) is True


def test_has_keys_for_false(inventory, registered_bestie):
    assert inventory.has_keys_for(registered_bestie) is False


def test_get_available_tiers(inventory, registered_bestie):
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.store_key(registered_bestie, "silver_001", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))
    tiers = inventory.get_available_tiers(registered_bestie)
    assert "GOLD" in tiers
    assert "SILVER" in tiers
    assert "BRONZE" not in tiers


# ── Edge Cases ──

def test_store_after_select_refills(inventory, registered_bestie):
    # Store, select all, verify empty, store again
    inventory.store_key(registered_bestie, "gold_001", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    inventory.select_coin(registered_bestie, "GOLD")
    assert inventory.has_keys_for(registered_bestie) is False

    inventory.store_key(registered_bestie, "gold_002", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
    assert inventory.has_keys_for(registered_bestie) is True


def test_priority_downgrade_then_select(inventory, registered_bestie):
    # Fill with Gold, downgrade to MATE (cap=0 Gold), try select
    for i in range(5):
        inventory.store_key(registered_bestie, f"gold_{i}", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))

    inventory.set_contact_priority(registered_bestie, "MATE")
    entry = inventory.select_coin(registered_bestie, "GOLD")
    assert entry is None
