"""Comprehensive tests for ContactsDatabase.

No Docker required — pure SQLite via tmp_path fixture.
"""

import pytest
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_contacts.models import Contact


# ─── CRUD basics ────────────────────────────────────────────────


class TestAddContact:

    def test_creates_stranger_by_default(self, db):
        """New contact always starts as STRANGER with zero counts."""
        c = db.add_contact("alice-001", "Alice")
        assert c.contact_id == "alice-001"
        assert c.display_name == "Alice"
        assert c.priority == "STRANGER"
        assert c.msg_count_total == 0
        assert c.msg_count_7d == 0
        assert c.msg_count_30d == 0
        assert c.priority_locked is False
        assert c.is_blocked is False

    def test_stores_signing_key(self, db):
        """Optional signing key is persisted."""
        key = b"\x01" * 32
        c = db.add_contact("bob-001", "Bob", signing_key=key)
        fetched = db.get_contact("bob-001")
        assert fetched.public_signing_key == key

    def test_idempotent_upsert(self, db):
        """Adding same contact_id twice updates display_name, no duplicate."""
        db.add_contact("alice-001", "Alice v1")
        db.add_contact("alice-001", "Alice v2")
        c = db.get_contact("alice-001")
        assert c.display_name == "Alice v2"
        assert len(db.get_all_contacts()) == 1


class TestGetContact:

    def test_existing(self, db):
        db.add_contact("alice-001", "Alice")
        c = db.get_contact("alice-001")
        assert isinstance(c, Contact)
        assert c.display_name == "Alice"

    def test_not_found(self, db):
        assert db.get_contact("nonexistent") is None

    def test_none_id(self, db):
        assert db.get_contact(None) is None


class TestRemoveContact:

    def test_removes_existing(self, db):
        db.add_contact("alice-001", "Alice")
        assert db.remove_contact("alice-001") is True
        assert db.get_contact("alice-001") is None

    def test_returns_false_for_missing(self, db):
        assert db.remove_contact("nonexistent") is False

    def test_cascades_message_log(self, db):
        """Deleting a contact also deletes their message_log rows (FK cascade)."""
        db.add_contact("alice-001", "Alice")
        db.record_message("alice-001", "SENT")
        db.record_message("alice-001", "RECEIVED")
        db.remove_contact("alice-001")
        # Verify message_log is empty for that contact
        db.cursor.execute(
            "SELECT COUNT(*) FROM message_log WHERE contact_id = ?",
            ("alice-001",),
        )
        assert db.cursor.fetchone()[0] == 0


# ─── Bulk queries ───────────────────────────────────────────────


class TestBulkQueries:

    def test_get_all_contacts(self, db):
        db.add_contact("a", "Alice")
        db.add_contact("b", "Bob")
        db.add_contact("c", "Charlie")
        assert len(db.get_all_contacts()) == 3

    def test_get_contacts_by_priority(self, db):
        db.add_contact("a", "Alice")
        db.add_contact("b", "Bob")
        # Both are STRANGER by default
        strangers = db.get_contacts_by_priority("STRANGER")
        assert len(strangers) == 2
        assert db.get_contacts_by_priority("BESTIE") == []

    def test_invalid_priority_returns_none(self, db):
        assert db.get_contacts_by_priority("PLATINUM") is None

    def test_search_contact(self, db):
        db.add_contact("a", "Alice")
        db.add_contact("b", "Bob")
        db.add_contact("c", "Alina")
        results = db.search_contact("Al")
        names = [c.display_name for c in results]
        assert "Alice" in names
        assert "Alina" in names
        assert "Bob" not in names

    def test_search_no_match(self, db):
        db.add_contact("a", "Alice")
        assert db.search_contact("Zz") == []


# ─── Message recording + auto-priority ──────────────────────────


class TestRecordMessage:

    def test_increments_counts_bidirectional(self, db):
        """Rolling counts only non-zero when both directions present."""
        db.add_contact("alice-001", "Alice")
        c = db.record_message("alice-001", "SENT")
        # Only one direction — rolling counts should be 0
        assert c.msg_count_total == 1
        assert c.msg_count_7d == 0
        assert c.msg_count_30d == 0
        assert c.last_msg_at is not None

        # Add a received message — now both directions present
        c = db.record_message("alice-001", "RECEIVED")
        assert c.msg_count_total == 2
        assert c.msg_count_7d == 2
        assert c.msg_count_30d == 2

    def test_one_sided_spam_does_not_promote(self, db):
        """Sending 10 messages without any reply should NOT promote."""
        db.add_contact("alice-001", "Alice")
        for _ in range(10):
            c = db.record_message("alice-001", "SENT")
        assert c.priority == "STRANGER"
        assert c.msg_count_7d == 0
        assert c.msg_count_30d == 0

    def test_one_sided_receive_does_not_promote(self, db):
        """Receiving 10 messages without sending any should NOT promote."""
        db.add_contact("alice-001", "Alice")
        for _ in range(10):
            c = db.record_message("alice-001", "RECEIVED")
        assert c.priority == "STRANGER"
        assert c.msg_count_7d == 0

    def test_auto_promotes_to_mate_bidirectional(self, db):
        """4+ messages in 30 days with BOTH directions → MATE."""
        db.add_contact("alice-001", "Alice")
        # 2 sent + 2 received = 4 total, both directions present
        db.record_message("alice-001", "SENT")
        db.record_message("alice-001", "RECEIVED")
        db.record_message("alice-001", "SENT")
        c = db.record_message("alice-001", "RECEIVED")
        assert c.priority == "MATE"

    def test_auto_promotes_to_bestie_bidirectional(self, db):
        """Balanced exchanges in 7 days → BESTIE (needs 3+3=6 paired count ≥ 5)."""
        db.add_contact("alice-001", "Alice")
        # 3 sent + 3 received → 2*min(3,3)=6 ≥ 5
        db.record_message("alice-001", "SENT")
        db.record_message("alice-001", "RECEIVED")
        db.record_message("alice-001", "SENT")
        db.record_message("alice-001", "RECEIVED")
        db.record_message("alice-001", "SENT")
        c = db.record_message("alice-001", "RECEIVED")
        assert c.priority == "BESTIE"

    def test_spam_then_single_reply_stays_stranger(self, db):
        """100 sent + 1 received must NOT promote (rolling count = 2, not 101)."""
        db.add_contact("alice-001", "Alice")
        for _ in range(100):
            db.record_message("alice-001", "SENT")
        c = db.record_message("alice-001", "RECEIVED")
        # 2*min(100,1) = 2, well below MATE threshold of 4
        assert c.priority == "STRANGER"
        assert c.msg_count_7d == 2
        assert c.msg_count_30d == 2

    def test_stays_stranger_below_threshold(self, db):
        """3 bidirectional messages isn't enough for MATE."""
        db.add_contact("alice-001", "Alice")
        db.record_message("alice-001", "SENT")
        db.record_message("alice-001", "RECEIVED")
        c = db.record_message("alice-001", "SENT")
        assert c.priority == "STRANGER"

    def test_invalid_direction_raises(self, db):
        db.add_contact("alice-001", "Alice")
        with pytest.raises(ValueError, match="Invalid direction"):
            db.record_message("alice-001", "UNKNOWN")

    def test_default_direction_is_sent(self, db):
        """Backwards compat: default direction is SENT."""
        db.add_contact("alice-001", "Alice")
        c = db.record_message("alice-001")
        assert c.msg_count_total == 1


# ─── Priority locking ───────────────────────────────────────────


class TestPriorityLock:

    def test_lock_sets_priority_and_flag(self, db):
        db.add_contact("alice-001", "Alice")
        c = db.lock_priority("alice-001", "BESTIE")
        assert c.priority == "BESTIE"
        assert c.priority_locked == 1  # SQLite stores booleans as 1/0

    def test_locked_priority_survives_messages(self, db):
        """Even 10 bidirectional messages shouldn't change a locked STRANGER."""
        db.add_contact("alice-001", "Alice")
        db.lock_priority("alice-001", "STRANGER")
        for _ in range(5):
            db.record_message("alice-001", "SENT")
            c = db.record_message("alice-001", "RECEIVED")
        assert c.priority == "STRANGER"

    def test_unlock_allows_recompute(self, db):
        """After unlock, next message triggers recompute."""
        db.add_contact("alice-001", "Alice")
        db.lock_priority("alice-001", "STRANGER")
        # Send 3 + receive 3 = 6 bidirectional while locked
        for _ in range(3):
            db.record_message("alice-001", "SENT")
            db.record_message("alice-001", "RECEIVED")
        c = db.get_contact("alice-001")
        assert c.priority == "STRANGER"  # still locked

        db.unlock_priority("alice-001")
        c = db.record_message("alice-001", "SENT")  # 7th message triggers recompute
        assert c.priority == "BESTIE"  # 7 msgs in 7d >= 5, both directions present

    def test_lock_invalid_priority_returns_none(self, db):
        db.add_contact("alice-001", "Alice")
        assert db.lock_priority("alice-001", "PLATINUM") is None

    def test_lock_none_id_returns_none(self, db):
        assert db.lock_priority(None, "BESTIE") is None

    def test_unlock_none_id_returns_none(self, db):
        assert db.unlock_priority(None) is None


# ─── Block + inactive ───────────────────────────────────────────


class TestBlockAndInactive:

    def test_block_contact(self, db):
        db.add_contact("alice-001", "Alice")
        db.block_contact("alice-001")
        c = db.get_contact("alice-001")
        assert c.is_blocked == 1  # SQLite stores booleans as 1/0

    def test_get_inactive_contacts(self, db):
        """Contacts with no messages should appear as inactive."""
        db.add_contact("a", "Alice")
        db.add_contact("b", "Bob")
        db.record_message("b", "SENT")  # Bob has activity
        inactive = db.get_inactive_contacts(days=30)
        ids = [c.contact_id for c in inactive]
        assert "a" in ids  # Alice never messaged → last_msg_at IS NULL


# ─── refresh_rolling_counts ─────────────────────────────────────


class TestRefreshRollingCounts:

    def test_returns_zero_when_no_changes(self, db):
        db.add_contact("alice-001", "Alice")
        assert db.refresh_rolling_counts() == 0

    def test_recomputes_after_messages(self, db):
        """Send enough bidirectional messages, refresh, verify priority updated."""
        db.add_contact("alice-001", "Alice")
        for _ in range(3):
            db.record_message("alice-001", "SENT")
            db.record_message("alice-001", "RECEIVED")
        # Already promoted by record_message (6 msgs, both dirs), so refresh returns 0
        assert db.refresh_rolling_counts() == 0
        c = db.get_contact("alice-001")
        assert c.priority == "BESTIE"
