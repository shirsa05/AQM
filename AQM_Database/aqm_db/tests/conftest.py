import os
import pytest
import fakeredis

from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.garbage_collector import GarbageCollector
from AQM_Database.aqm_db.stats import StorageReporter


@pytest.fixture
def vault_client():
    r = fakeredis.FakeRedis()
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def inventory_client():
    r = fakeredis.FakeRedis()
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def vault(vault_client):
    return SecureVault(vault_client)


@pytest.fixture
def inventory(inventory_client):
    return SmartInventory(inventory_client)


@pytest.fixture
def gc(inventory, inventory_client):
    return GarbageCollector(inventory, inventory_client)


@pytest.fixture
def reporter(vault, inventory):
    return StorageReporter(vault, inventory)


@pytest.fixture
def sample_gold_key():
    return {
        "key_id": "GOLD_KEY_001",
        "coin_category": "GOLD",
        "encrypted_blob": os.urandom(1200),
        "encryption_iv": os.urandom(12),
        "auth_tag": os.urandom(16),
        "coin_version": "kyber768_v1",
    }


@pytest.fixture
def sample_silver_key():
    return {
        "key_id": "SILVER_KEY_001",
        "coin_category": "SILVER",
        "encrypted_blob": os.urandom(1200),
        "encryption_iv": os.urandom(12),
        "auth_tag": os.urandom(16),
        "coin_version": "kyber768_v1",
    }


@pytest.fixture
def sample_bronze_key():
    return {
        "key_id": "BRONZE_KEY_001",
        "coin_category": "BRONZE",
        "encrypted_blob": os.urandom(50),
        "encryption_iv": os.urandom(12),
        "auth_tag": os.urandom(16),
        "coin_version": "x25519_v1",
    }


@pytest.fixture
def registered_bestie(inventory):
    contact_id = "bob_test_uuid"
    inventory.register_contact(contact_id, "BESTIE", "Bob")
    return contact_id


@pytest.fixture
def registered_mate(inventory):
    contact_id = "charlie_test_uuid"
    inventory.register_contact(contact_id, "MATE", "Charlie")
    return contact_id
