import os
import time

import pytest

from AQM_Database.aqm_shared import errors, config
from AQM_Database.aqm_shared.types import VaultEntry, VaultStats


# ── Store ──

def test_store_key_success(vault, sample_gold_key):
    assert vault.store_key(**sample_gold_key) is True


def test_store_key_duplicate_raises(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    with pytest.raises(errors.KeyAlreadyExistsError):
        vault.store_key(**sample_gold_key)


def test_store_key_invalid_category_raises(vault):
    with pytest.raises(errors.InvalidCoinCategoryError):
        vault.store_key("k1", "PLATINUM", os.urandom(10), os.urandom(12), os.urandom(16))


def test_store_key_sets_ttl(vault, vault_client, sample_gold_key):
    vault.store_key(**sample_gold_key)
    full_key = vault._vault_key(sample_gold_key["key_id"])
    ttl = vault_client.ttl(full_key)
    assert ttl > 0
    assert ttl <= config.VAULT_KEY_TTL_SECONDS


def test_store_key_increments_stats(vault, sample_gold_key, sample_silver_key):
    vault.store_key(**sample_gold_key)
    vault.store_key(**sample_silver_key)
    stats = vault.get_stats()
    assert stats.active_gold == 1
    assert stats.active_silver == 1


# ── Fetch ──

def test_fetch_existing_key(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    entry = vault.fetch_key(sample_gold_key["key_id"])
    assert entry is not None
    assert isinstance(entry, VaultEntry)
    assert entry.key_id == sample_gold_key["key_id"]
    assert entry.coin_category == "GOLD"
    assert entry.status == "ACTIVE"


def test_fetch_nonexistent_returns_none(vault):
    assert vault.fetch_key("nonexistent_key") is None


def test_fetch_burned_key_returns_none(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    assert vault.fetch_key(sample_gold_key["key_id"]) is None


def test_fetch_preserves_binary_blobs(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    entry = vault.fetch_key(sample_gold_key["key_id"])
    assert entry.encrypted_blob == sample_gold_key["encrypted_blob"]
    assert entry.encryption_iv == sample_gold_key["encryption_iv"]
    assert entry.auth_tag == sample_gold_key["auth_tag"]


# ── Burn ──

def test_burn_key_success(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    assert vault.burn_key(sample_gold_key["key_id"]) is True


def test_burn_nonexistent_raises(vault):
    with pytest.raises(errors.KeyNotFoundError):
        vault.burn_key("nonexistent_key")


def test_burn_already_burned_raises(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    with pytest.raises(errors.KeyAlreadyBurnedError):
        vault.burn_key(sample_gold_key["key_id"])


def test_burn_sets_short_ttl(vault, vault_client, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    full_key = vault._vault_key(sample_gold_key["key_id"])
    ttl = vault_client.ttl(full_key)
    assert 0 < ttl <= config.VAULT_BURN_GRACE_SECONDS


def test_burn_decrements_active_count(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    assert vault.count_active("GOLD") == 1
    vault.burn_key(sample_gold_key["key_id"])
    assert vault.count_active("GOLD") == 0


def test_burn_increments_burned_count(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    stats = vault.get_stats()
    assert stats.total_burned == 1


# ── Count ──

def test_count_active_all_tiers(vault, sample_gold_key, sample_silver_key):
    vault.store_key(**sample_gold_key)
    vault.store_key(**sample_silver_key)
    counts = vault.count_active()
    assert isinstance(counts, dict)
    assert counts["GOLD"] == 1
    assert counts["SILVER"] == 1
    assert counts["BRONZE"] == 0


def test_count_active_single_tier(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    assert vault.count_active("GOLD") == 1
    assert vault.count_active("SILVER") == 0


def test_count_active_empty_vault(vault):
    counts = vault.count_active()
    assert counts == {"GOLD": 0, "SILVER": 0, "BRONZE": 0}


# ── Exists ──

def test_exists_true(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    assert vault.exists(sample_gold_key["key_id"]) is True


def test_exists_false(vault):
    assert vault.exists("nonexistent") is False


# ── Get All Active IDs ──

def test_get_all_active_ids(vault, sample_gold_key, sample_silver_key):
    vault.store_key(**sample_gold_key)
    vault.store_key(**sample_silver_key)
    ids = vault.get_all_active_ids()
    assert set(ids) == {sample_gold_key["key_id"], sample_silver_key["key_id"]}


def test_get_all_active_ids_filtered(vault, sample_gold_key, sample_silver_key):
    vault.store_key(**sample_gold_key)
    vault.store_key(**sample_silver_key)
    ids = vault.get_all_active_ids("GOLD")
    assert ids == [sample_gold_key["key_id"]]


def test_get_all_active_ids_excludes_burned(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    ids = vault.get_all_active_ids()
    assert ids == []


# ── Purge ──

def test_purge_expired_removes_old_keys(vault, vault_client):
    key_id = "OLD_KEY_001"
    full_key = vault._vault_key(key_id)
    old_ts = str(int((time.time() - 31 * 86400) * 1000))  # 31 days ago
    vault_client.hset(full_key, mapping={
        "key_id": key_id,
        "coin_category": "GOLD",
        "encrypted_blob": os.urandom(100),
        "encryption_iv": os.urandom(12),
        "auth_tag": os.urandom(16),
        "coin_version": "kyber768_v1",
        "status": "ACTIVE",
        "created_at": old_ts,
    })
    vault_client.hincrby(config.VAULT_STATS_KEY, "active_gold", 1)

    purged = vault.purge_expired(max_age_days=30)
    assert purged == 1
    assert vault.fetch_key(key_id) is None


def test_purge_expired_keeps_recent_keys(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    purged = vault.purge_expired(max_age_days=30)
    assert purged == 0
    assert vault.fetch_key(sample_gold_key["key_id"]) is not None


def test_purge_updates_stats(vault, vault_client):
    key_id = "OLD_KEY_002"
    full_key = vault._vault_key(key_id)
    old_ts = str(int((time.time() - 31 * 86400) * 1000))
    vault_client.hset(full_key, mapping={
        "key_id": key_id,
        "coin_category": "SILVER",
        "encrypted_blob": os.urandom(100),
        "encryption_iv": os.urandom(12),
        "auth_tag": os.urandom(16),
        "coin_version": "kyber768_v1",
        "status": "ACTIVE",
        "created_at": old_ts,
    })
    vault_client.hincrby(config.VAULT_STATS_KEY, "active_silver", 1)

    vault.purge_expired(max_age_days=30)
    stats = vault.get_stats()
    assert stats.active_silver == 0
    assert stats.total_expired == 1


# ── Stats ──

def test_get_stats_empty(vault):
    stats = vault.get_stats()
    assert isinstance(stats, VaultStats)
    assert stats.active_gold == 0
    assert stats.total_burned == 0


def test_get_stats_after_operations(vault, sample_gold_key):
    vault.store_key(**sample_gold_key)
    vault.burn_key(sample_gold_key["key_id"])
    stats = vault.get_stats()
    assert stats.active_gold == 0
    assert stats.total_burned == 1
