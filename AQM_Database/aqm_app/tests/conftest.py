import pytest
from unittest.mock import MagicMock, AsyncMock
from dataclasses import dataclass

from AQM_Database.aqm_app.orchestrator import AQMApp


@dataclass
class FakeMintedCoinBundle:
    key_id: str
    coin_category: str
    public_key: bytes
    secret_key: bytes
    signature: bytes
    signing_public_key: bytes = None


@dataclass
class FakeVaultEntry:
    key_id: str
    coin_category: str
    encrypted_blob: bytes
    encryption_iv: bytes
    auth_tag: bytes
    status: str = "ACTIVE"
    created_at: int = 0
    coin_version: str = "kyber768_v1"


@dataclass
class FakeInventoryEntry:
    contact_id: str
    key_id: str
    coin_category: str
    public_key: bytes
    signature: bytes
    fetched_at: int = 0


@dataclass
class FakeContact:
    contact_id: str
    display_name: str
    priority: str = "STRANGER"
    public_signing_key: bytes = None
    msg_count_total: int = 0
    msg_count_7d: int = 0
    msg_count_30d: int = 0
    priority_locked: bool = False
    is_blocked: bool = False


def _make_mock_subsystems():
    """Create a dict of mock subsystems for AQMApp injection."""
    vault = MagicMock()
    vault.store_key.return_value = True
    vault.burn_key.return_value = True
    vault.count_active.return_value = 0

    inventory = MagicMock()
    inventory.register_contact.return_value = True
    inventory.consume_key.return_value = True

    contacts = MagicMock()

    session_store = MagicMock()
    session_store.load_ratchet.return_value = None

    network = MagicMock()
    network.connect = AsyncMock()
    network.send_parcel = AsyncMock()
    network.disconnect = AsyncMock()
    network.on_message = MagicMock()

    crypto = MagicMock()
    crypto.encrypt_aead.return_value = b"\x00" * 12 + b"ciphertext_here!" + b"\x01" * 16

    context = MagicMock()

    return {
        "vault": vault,
        "inventory": inventory,
        "contacts": contacts,
        "session_store": session_store,
        "network": network,
        "crypto": crypto,
        "context": context,
    }


@pytest.fixture
def mock_subs():
    return _make_mock_subsystems()


@pytest.fixture
def app(mock_subs):
    return AQMApp("alice", "ws://localhost:9000", **mock_subs)
