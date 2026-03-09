"""Comprehensive tests for SessionRatchet + SessionStore.

No Docker required — pure HKDF derivation + SQLite via tmp_path.
"""

import pytest
import os
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_session.session_store import SessionStore


def exhaust_ratchet(ratchet: SessionRatchet):
    """Derives keys until the ratchet hits its dynamic limit."""
    limit = ratchet.max_messages
    for _ in range(limit):
        ratchet.derive_message_key()


# ════════════════════════════════════════════════════════════════
#  CORE: Key derivation fundamentals
# ════════════════════════════════════════════════════════════════


class TestKeyDerivation:

    def test_derived_key_is_32_bytes(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        key = r.derive_message_key()
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_consecutive_keys_are_unique(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        keys = [r.derive_message_key() for _ in range(10)]
        assert len(set(keys)) == 10

    def test_counter_increments(self):
        r = SessionRatchet("alice", "SILVER", os.urandom(32))
        assert r.msg_counter == 0
        r.derive_message_key()
        assert r.msg_counter == 1
        r.derive_message_key()
        assert r.msg_counter == 2

    def test_chain_key_advances_each_step(self):
        """current_chain_key must change after each derivation (forward secrecy)."""
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        ck_before = r.current_chain_key
        r.derive_message_key()
        assert r.current_chain_key != ck_before

    def test_same_master_secret_produces_same_keys(self):
        """Deterministic: same master → same chain → same message keys."""
        secret = os.urandom(32)
        r1 = SessionRatchet("alice", "GOLD", secret)
        r2 = SessionRatchet("alice", "GOLD", secret)
        for _ in range(5):
            assert r1.derive_message_key() == r2.derive_message_key()

    def test_different_master_secrets_produce_different_keys(self):
        r1 = SessionRatchet("alice", "GOLD", os.urandom(32))
        r2 = SessionRatchet("alice", "GOLD", os.urandom(32))
        assert r1.derive_message_key() != r2.derive_message_key()

    def test_different_contact_ids_same_secret_same_keys(self):
        """contact_id is metadata — derivation depends only on master_secret."""
        secret = os.urandom(32)
        r1 = SessionRatchet("alice", "GOLD", secret)
        r2 = SessionRatchet("bob", "GOLD", secret)
        assert r1.derive_message_key() == r2.derive_message_key()


# ════════════════════════════════════════════════════════════════
#  TIER LIMITS: Dynamic window sizes
# ════════════════════════════════════════════════════════════════


class TestTierLimits:

    def test_gold_limit(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        assert r.max_messages == 250

    def test_silver_limit(self):
        r = SessionRatchet("a", "SILVER", os.urandom(32))
        assert r.max_messages == 150

    def test_bronze_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        assert r.max_messages == 75

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError, match="Invalid coin tier"):
            SessionRatchet("a", "PLATINUM", os.urandom(32))

    def test_needs_rekey_false_before_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        for _ in range(74):
            r.derive_message_key()
        assert r.needs_rekey() is False

    def test_needs_rekey_true_at_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        assert r.needs_rekey() is True

    def test_derive_after_exhaustion_raises(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_message_key()


# ════════════════════════════════════════════════════════════════
#  REKEY: Consuming a new coin
# ════════════════════════════════════════════════════════════════


class TestRekey:

    def test_rekey_resets_counter(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        for _ in range(10):
            r.derive_message_key()
        r.rekey(os.urandom(32), "GOLD")
        assert r.msg_counter == 0

    def test_rekey_changes_tier(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75

    def test_rekey_produces_new_chain(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        key_before = r.derive_message_key()
        r.rekey(os.urandom(32), "GOLD")
        key_after = r.derive_message_key()
        assert key_before != key_after

    def test_rekey_invalid_tier_raises(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        with pytest.raises(ValueError, match="Invalid coin tier"):
            r.rekey(os.urandom(32), "DIAMOND")

    def test_rekey_unlocks_exhausted_ratchet(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        assert r.needs_rekey() is True
        r.rekey(os.urandom(32), "SILVER")
        assert r.needs_rekey() is False
        key = r.derive_message_key()
        assert len(key) == 32


# ════════════════════════════════════════════════════════════════
#  STATE: Serialization + restoration
# ════════════════════════════════════════════════════════════════


class TestState:

    def test_get_state_returns_dict(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        state = r.get_state()
        assert isinstance(state, dict)
        assert state["contact_id"] == "alice"
        assert state["coin_tier"] == "GOLD"
        assert state["msg_counter"] == 0
        assert isinstance(state["current_chain_key"], str)  # hex

    def test_get_state_uninitialized_raises(self):
        r = SessionRatchet("alice", "GOLD")  # no master_secret
        with pytest.raises(ValueError, match="not initialized"):
            r.get_state()

    def test_from_state_roundtrip(self):
        """Save state → restore → derive same next key."""
        secret = os.urandom(32)
        r1 = SessionRatchet("alice", "SILVER", secret)
        for _ in range(5):
            r1.derive_message_key()

        state = r1.get_state()
        r2 = SessionRatchet.from_state(state)

        assert r2.contact_id == "alice"
        assert r2.coin_tier == "SILVER"
        assert r2.msg_counter == 5
        # Both must produce the same next key
        assert r1.derive_message_key() == r2.derive_message_key()

    def test_from_state_preserves_tier_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        state = r.get_state()
        restored = SessionRatchet.from_state(state)
        assert restored.max_messages == 75


# ════════════════════════════════════════════════════════════════
#  SESSION STORE: SQLite persistence
# ════════════════════════════════════════════════════════════════


class TestSessionStore:

    @pytest.fixture
    def store(self, tmp_path):
        return SessionStore(db_path=str(tmp_path / "sessions.db"))

    def test_save_and_load(self, store):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        r.derive_message_key()
        store.save_ratchet(r)

        loaded = store.load_ratchet("alice")
        assert loaded is not None
        assert loaded.contact_id == "alice"
        assert loaded.coin_tier == "GOLD"
        assert loaded.msg_counter == 1

    def test_load_nonexistent_returns_none(self, store):
        assert store.load_ratchet("nobody") is None

    def test_save_overwrites_on_conflict(self, store):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        store.save_ratchet(r)

        r.derive_message_key()
        r.derive_message_key()
        store.save_ratchet(r)

        loaded = store.load_ratchet("alice")
        assert loaded.msg_counter == 2

    def test_delete_ratchet(self, store):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        store.save_ratchet(r)
        store.delete_ratchet("alice")
        assert store.load_ratchet("alice") is None

    def test_delete_nonexistent_no_error(self, store):
        store.delete_ratchet("nobody")  # should not raise

    def test_restored_ratchet_derives_correctly(self, store):
        """Full cycle: create → derive 3 keys → save → load → derive next key matches."""
        secret = os.urandom(32)
        r1 = SessionRatchet("bob", "SILVER", secret)
        for _ in range(3):
            r1.derive_message_key()
        store.save_ratchet(r1)

        r2 = store.load_ratchet("bob")
        # Both should produce identical next key
        assert r1.derive_message_key() == r2.derive_message_key()

    def test_multiple_contacts(self, store):
        r_alice = SessionRatchet("alice", "GOLD", os.urandom(32))
        r_bob = SessionRatchet("bob", "BRONZE", os.urandom(32))
        store.save_ratchet(r_alice)
        store.save_ratchet(r_bob)

        assert store.load_ratchet("alice").coin_tier == "GOLD"
        assert store.load_ratchet("bob").coin_tier == "BRONZE"


# ════════════════════════════════════════════════════════════════
#  SCENARIOS: Tier degradation/recovery (from coworker's tests)
# ════════════════════════════════════════════════════════════════


class TestBestieScenarios:

    def test_degradation_gold_to_bronze(self):
        r = SessionRatchet("bestie-uuid", "GOLD", os.urandom(32))
        assert r.max_messages == 250
        exhaust_ratchet(r)
        assert r.needs_rekey() is True

        r.rekey(os.urandom(32), "SILVER")
        assert r.coin_tier == "SILVER"
        assert r.max_messages == 150
        assert r.msg_counter == 0
        exhaust_ratchet(r)
        assert r.needs_rekey() is True

        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75
        exhaust_ratchet(r)

        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_message_key()

    def test_recovery_silver_to_gold(self):
        r = SessionRatchet("bestie-uuid", "SILVER", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "GOLD")
        assert r.coin_tier == "GOLD"
        assert r.max_messages == 250
        key = r.derive_message_key()
        assert len(key) == 32
        assert r.msg_counter == 1


class TestMateScenarios:

    def test_degradation_silver_to_bronze(self):
        r = SessionRatchet("mate-uuid", "SILVER", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75
        assert len(r.derive_message_key()) == 32

    def test_recovery_bronze_to_silver(self):
        r = SessionRatchet("mate-uuid", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "SILVER")
        assert r.coin_tier == "SILVER"
        assert r.max_messages == 150
        assert len(r.derive_message_key()) == 32


class TestStrangerScenarios:

    def test_bronze_only_strict_limit(self):
        r = SessionRatchet("stranger-uuid", "BRONZE", os.urandom(32))
        assert r.max_messages == 75

        for _ in range(74):
            r.derive_message_key()
        assert r.needs_rekey() is False

        r.derive_message_key()
        assert r.needs_rekey() is True

        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_message_key()