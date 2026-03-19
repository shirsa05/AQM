"""Comprehensive tests for SessionRatchet + SessionStore.

No Docker required — pure HKDF derivation + SQLite via tmp_path.
"""

import pytest
import os
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_session.session_store import SessionStore


def exhaust_ratchet(ratchet: SessionRatchet):
    """Derives send keys until the ratchet hits its dynamic limit."""
    limit = ratchet.max_messages
    for _ in range(limit):
        ratchet.derive_send_key()


# ════════════════════════════════════════════════════════════════
#  CORE: Key derivation fundamentals
# ════════════════════════════════════════════════════════════════


class TestKeyDerivation:

    def test_derived_key_is_32_bytes(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        key = r.derive_send_key()
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_consecutive_keys_are_unique(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        keys = [r.derive_send_key() for _ in range(10)]
        assert len(set(keys)) == 10

    def test_send_counter_increments(self):
        r = SessionRatchet("alice", "SILVER", os.urandom(32))
        assert r.send_counter == 0
        r.derive_send_key()
        assert r.send_counter == 1
        r.derive_send_key()
        assert r.send_counter == 2

    def test_recv_counter_increments_independently(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        r.derive_send_key()
        r.derive_send_key()
        assert r.send_counter == 2
        assert r.recv_counter == 0
        r.derive_recv_key()
        assert r.recv_counter == 1
        assert r.send_counter == 2  # unchanged

    def test_chain_key_advances_each_step(self):
        """send_chain_key must change after each derivation (forward secrecy)."""
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        ck_before = r.send_chain_key
        r.derive_send_key()
        assert r.send_chain_key != ck_before

    def test_recv_chain_key_advances(self):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        ck_before = r.recv_chain_key
        r.derive_recv_key()
        assert r.recv_chain_key != ck_before

    def test_send_recv_chains_are_different(self):
        """Send and receive chains must derive different keys."""
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        send_key = r.derive_send_key()
        recv_key = r.derive_recv_key()
        assert send_key != recv_key

    def test_initiator_send_matches_responder_recv(self):
        """Initiator's send key N must equal responder's recv key N."""
        secret = os.urandom(32)
        alice = SessionRatchet("bob", "GOLD", secret, is_initiator=True)
        bob = SessionRatchet("alice", "GOLD", secret, is_initiator=False)

        for _ in range(5):
            send_key = alice.derive_send_key()
            recv_key = bob.derive_recv_key()
            assert send_key == recv_key

    def test_responder_send_matches_initiator_recv(self):
        """Responder's send key N must equal initiator's recv key N."""
        secret = os.urandom(32)
        alice = SessionRatchet("bob", "GOLD", secret, is_initiator=True)
        bob = SessionRatchet("alice", "GOLD", secret, is_initiator=False)

        for _ in range(5):
            send_key = bob.derive_send_key()
            recv_key = alice.derive_recv_key()
            assert send_key == recv_key

    def test_interleaved_send_recv(self):
        """Simulate real chat: Alice sends, Bob receives, Bob sends, Alice receives."""
        secret = os.urandom(32)
        alice = SessionRatchet("bob", "GOLD", secret, is_initiator=True)
        bob = SessionRatchet("alice", "GOLD", secret, is_initiator=False)

        # Alice sends 3 messages
        for _ in range(3):
            k = alice.derive_send_key()
            assert k == bob.derive_recv_key()

        # Bob sends 2 messages
        for _ in range(2):
            k = bob.derive_send_key()
            assert k == alice.derive_recv_key()

        # Alice sends 1 more
        k = alice.derive_send_key()
        assert k == bob.derive_recv_key()

        assert alice.send_counter == 4
        assert alice.recv_counter == 2
        assert bob.send_counter == 2
        assert bob.recv_counter == 4

    def test_different_master_secrets_produce_different_keys(self):
        r1 = SessionRatchet("alice", "GOLD", os.urandom(32))
        r2 = SessionRatchet("alice", "GOLD", os.urandom(32))
        assert r1.derive_send_key() != r2.derive_send_key()

    def test_legacy_derive_message_key_calls_send(self):
        """derive_message_key() is an alias for derive_send_key()."""
        secret = os.urandom(32)
        r1 = SessionRatchet("alice", "GOLD", secret)
        r2 = SessionRatchet("alice", "GOLD", secret)
        assert r1.derive_message_key() == r2.derive_send_key()


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
            r.derive_send_key()
        assert r.needs_rekey() is False

    def test_needs_rekey_true_at_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        assert r.needs_rekey() is True

    def test_derive_after_exhaustion_raises(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_send_key()

    def test_recv_not_limited_by_send_counter(self):
        """Receive chain is independent — can receive even if send is exhausted."""
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)
        assert r.needs_rekey() is True
        # Can still derive recv keys
        key = r.derive_recv_key()
        assert len(key) == 32


# ════════════════════════════════════════════════════════════════
#  REKEY: Consuming a new coin
# ════════════════════════════════════════════════════════════════


class TestRekey:

    def test_rekey_resets_both_counters(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        for _ in range(10):
            r.derive_send_key()
        for _ in range(5):
            r.derive_recv_key()
        r.rekey(os.urandom(32), "GOLD")
        assert r.send_counter == 0
        assert r.recv_counter == 0

    def test_rekey_changes_tier(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75

    def test_rekey_produces_new_chain(self):
        r = SessionRatchet("a", "GOLD", os.urandom(32))
        key_before = r.derive_send_key()
        r.rekey(os.urandom(32), "GOLD")
        key_after = r.derive_send_key()
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
        key = r.derive_send_key()
        assert len(key) == 32

    def test_rekey_preserves_initiator_responder_symmetry(self):
        """After rekey, initiator/responder chains still match."""
        secret1 = os.urandom(32)
        alice = SessionRatchet("bob", "GOLD", secret1, is_initiator=True)
        bob = SessionRatchet("alice", "GOLD", secret1, is_initiator=False)

        # Use some keys
        alice.derive_send_key()
        bob.derive_recv_key()

        # Rekey with new secret
        secret2 = os.urandom(32)
        alice.rekey(secret2, "SILVER", is_initiator=True)
        bob.rekey(secret2, "SILVER", is_initiator=False)

        # Should still be in sync
        for _ in range(3):
            assert alice.derive_send_key() == bob.derive_recv_key()
            assert bob.derive_send_key() == alice.derive_recv_key()


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
        assert state["send_counter"] == 0
        assert state["recv_counter"] == 0
        assert isinstance(state["send_chain_key"], str)  # hex
        assert isinstance(state["recv_chain_key"], str)  # hex
        assert state["is_initiator"] is True

    def test_get_state_uninitialized_raises(self):
        r = SessionRatchet("alice", "GOLD")  # no master_secret
        with pytest.raises(ValueError, match="not initialized"):
            r.get_state()

    def test_from_state_roundtrip(self):
        """Save state → restore → derive same next key."""
        secret = os.urandom(32)
        r1 = SessionRatchet("alice", "SILVER", secret)
        for _ in range(5):
            r1.derive_send_key()
        for _ in range(3):
            r1.derive_recv_key()

        state = r1.get_state()
        r2 = SessionRatchet.from_state(state)

        assert r2.contact_id == "alice"
        assert r2.coin_tier == "SILVER"
        assert r2.send_counter == 5
        assert r2.recv_counter == 3
        # Both must produce the same next keys
        assert r1.derive_send_key() == r2.derive_send_key()
        assert r1.derive_recv_key() == r2.derive_recv_key()

    def test_from_state_preserves_tier_limit(self):
        r = SessionRatchet("a", "BRONZE", os.urandom(32))
        state = r.get_state()
        restored = SessionRatchet.from_state(state)
        assert restored.max_messages == 75

    def test_legacy_state_migration(self):
        """Old-format state (single chain) can be loaded."""
        legacy_state = {
            "contact_id": "alice",
            "coin_tier": "GOLD",
            "msg_counter": 3,
            "current_chain_key": os.urandom(32).hex(),
        }
        r = SessionRatchet.from_state(legacy_state)
        assert r.send_counter == 3
        assert r.recv_counter == 0
        assert r.send_chain_key is not None


# ════════════════════════════════════════════════════════════════
#  SESSION STORE: SQLite persistence
# ════════════════════════════════════════════════════════════════


class TestSessionStore:

    @pytest.fixture
    def store(self, tmp_path):
        return SessionStore(db_path=str(tmp_path / "sessions.db"))

    def test_save_and_load(self, store):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        r.derive_send_key()
        store.save_ratchet(r)

        loaded = store.load_ratchet("alice")
        assert loaded is not None
        assert loaded.contact_id == "alice"
        assert loaded.coin_tier == "GOLD"
        assert loaded.send_counter == 1

    def test_load_nonexistent_returns_none(self, store):
        assert store.load_ratchet("nobody") is None

    def test_save_overwrites_on_conflict(self, store):
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        store.save_ratchet(r)

        r.derive_send_key()
        r.derive_send_key()
        store.save_ratchet(r)

        loaded = store.load_ratchet("alice")
        assert loaded.send_counter == 2

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
            r1.derive_send_key()
        store.save_ratchet(r1)

        r2 = store.load_ratchet("bob")
        # Both should produce identical next key
        assert r1.derive_send_key() == r2.derive_send_key()

    def test_multiple_contacts(self, store):
        r_alice = SessionRatchet("alice", "GOLD", os.urandom(32))
        r_bob = SessionRatchet("bob", "BRONZE", os.urandom(32))
        store.save_ratchet(r_alice)
        store.save_ratchet(r_bob)

        assert store.load_ratchet("alice").coin_tier == "GOLD"
        assert store.load_ratchet("bob").coin_tier == "BRONZE"

    def test_save_load_preserves_recv_counter(self, store):
        """recv_counter must survive save/load cycle."""
        r = SessionRatchet("alice", "GOLD", os.urandom(32))
        r.derive_send_key()
        r.derive_send_key()
        r.derive_recv_key()
        r.derive_recv_key()
        r.derive_recv_key()
        store.save_ratchet(r)

        loaded = store.load_ratchet("alice")
        assert loaded.send_counter == 2
        assert loaded.recv_counter == 3


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
        assert r.send_counter == 0
        exhaust_ratchet(r)
        assert r.needs_rekey() is True

        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75
        exhaust_ratchet(r)

        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_send_key()

    def test_recovery_silver_to_gold(self):
        r = SessionRatchet("bestie-uuid", "SILVER", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "GOLD")
        assert r.coin_tier == "GOLD"
        assert r.max_messages == 250
        key = r.derive_send_key()
        assert len(key) == 32
        assert r.send_counter == 1


class TestMateScenarios:

    def test_degradation_silver_to_bronze(self):
        r = SessionRatchet("mate-uuid", "SILVER", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "BRONZE")
        assert r.coin_tier == "BRONZE"
        assert r.max_messages == 75
        assert len(r.derive_send_key()) == 32

    def test_recovery_bronze_to_silver(self):
        r = SessionRatchet("mate-uuid", "BRONZE", os.urandom(32))
        exhaust_ratchet(r)

        r.rekey(os.urandom(32), "SILVER")
        assert r.coin_tier == "SILVER"
        assert r.max_messages == 150
        assert len(r.derive_send_key()) == 32


class TestStrangerScenarios:

    def test_bronze_only_strict_limit(self):
        r = SessionRatchet("stranger-uuid", "BRONZE", os.urandom(32))
        assert r.max_messages == 75

        for _ in range(74):
            r.derive_send_key()
        assert r.needs_rekey() is False

        r.derive_send_key()
        assert r.needs_rekey() is True

        with pytest.raises(ValueError, match="Ratchet exhausted"):
            r.derive_send_key()
