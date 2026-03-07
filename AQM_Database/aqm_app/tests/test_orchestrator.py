import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call
from AQM_Database.aqm_app.orchestrator import AQMApp, _cap_tier
from AQM_Database.aqm_app.tests.conftest import (
    FakeMintedCoinBundle,
    FakeVaultEntry,
    FakeInventoryEntry,
    FakeContact,
)

pytestmark = pytest.mark.asyncio


# ── Construction ────────────────────────────────────────────────────


class TestInit:
    def test_wires_subsystems(self, app, mock_subs):
        assert app.vault is mock_subs["vault"]
        assert app.inventory is mock_subs["inventory"]
        assert app.contacts is mock_subs["contacts"]
        assert app.session_store is mock_subs["session_store"]
        assert app.network is mock_subs["network"]
        assert app.crypto is mock_subs["crypto"]
        assert app.context is mock_subs["context"]

    def test_user_id_stored(self, app):
        assert app.user_id == "alice"

    def test_active_sessions_empty(self, app):
        assert app.active_sessions == {}


# ── Start ───────────────────────────────────────────────────────────


class TestStart:
    async def test_start_registers_callback_and_connects(self, app, mock_subs):
        mock_subs["vault"].count_active.return_value = 99  # skip minting
        mock_subs["crypto"].mint_coin.return_value = FakeMintedCoinBundle(
            key_id="k1", coin_category="GOLD",
            public_key=b"pk", secret_key=b"sk",
            signature=b"sig",
        )
        await app.start()
        mock_subs["network"].on_message.assert_called_once()
        mock_subs["network"].connect.assert_awaited_once()


# ── Minting ─────────────────────────────────────────────────────────


class TestMintCoins:
    async def test_mints_needed_coins(self, app, mock_subs):
        mock_subs["vault"].count_active.return_value = 0
        mock_subs["crypto"].mint_coin.return_value = FakeMintedCoinBundle(
            key_id="coin-1", coin_category="GOLD",
            public_key=b"pk" * 100, secret_key=b"sk" * 100,
            signature=b"sig" * 50,
        )
        mock_subs["crypto"].encrypt_aead.return_value = (
            b"\x00" * 12 + b"encrypted" + b"\x01" * 16
        )

        result = await app.mint_coins()

        # BUDGET_CAPS["BESTIE"] = {GOLD: 5, SILVER: 4, BRONZE: 1} = 10 total
        assert mock_subs["crypto"].mint_coin.call_count == 10
        assert mock_subs["vault"].store_key.call_count == 10
        assert len(result) == 10

    async def test_skips_already_minted(self, app, mock_subs):
        mock_subs["vault"].count_active.return_value = 99
        result = await app.mint_coins()
        mock_subs["crypto"].mint_coin.assert_not_called()
        assert len(result) == 0

    async def test_vault_store_receives_split_blob(self, app, mock_subs):
        mock_subs["vault"].count_active.return_value = 0
        mock_subs["crypto"].mint_coin.return_value = FakeMintedCoinBundle(
            key_id="k1", coin_category="GOLD",
            public_key=b"pk", secret_key=b"sk",
            signature=b"sig",
        )
        # Return known blob: 12-byte nonce + body + 16-byte tag
        nonce = b"\xaa" * 12
        body = b"the_encrypted_body"
        tag = b"\xbb" * 16
        mock_subs["crypto"].encrypt_aead.return_value = nonce + body + tag

        await app.mint_coins()

        _, kwargs = mock_subs["vault"].store_key.call_args
        assert kwargs["encryption_iv"] == nonce
        assert kwargs["encrypted_blob"] == body
        assert kwargs["auth_tag"] == tag


# ── Add Contact ─────────────────────────────────────────────────────


class TestAddContact:
    async def test_adds_contact_and_registers_inventory(self, app, mock_subs):
        mock_subs["contacts"].add_contact.return_value = FakeContact(
            contact_id="bob", display_name="Bob", priority="STRANGER"
        )
        result = await app.add_contact("bob", "Bob")
        mock_subs["contacts"].add_contact.assert_called_once_with("bob", "Bob", None)
        mock_subs["inventory"].register_contact.assert_called_once_with(
            "bob", "STRANGER", "Bob"
        )
        assert result.contact_id == "bob"

    async def test_add_contact_with_signing_key(self, app, mock_subs):
        mock_subs["contacts"].add_contact.return_value = FakeContact(
            contact_id="bob", display_name="Bob"
        )
        await app.add_contact("bob", "Bob", signing_key=b"pubkey")
        mock_subs["contacts"].add_contact.assert_called_once_with("bob", "Bob", b"pubkey")


# ── Send Message ────────────────────────────────────────────────────


class TestSendMessage:
    @pytest.fixture
    def send_app(self, app, mock_subs):
        """Pre-configure mocks for a successful send."""
        mock_subs["contacts"].get_contact.return_value = FakeContact(
            contact_id="bob", display_name="Bob", priority="BESTIE"
        )
        mock_subs["context"].select_coin.return_value = "GOLD"
        mock_subs["inventory"].select_coin.return_value = FakeInventoryEntry(
            contact_id="bob", key_id="coin-42", coin_category="GOLD",
            public_key=b"their_pk", signature=b"sig",
        )
        mock_subs["crypto"].kem_encapsulate.return_value = (b"kem_ct", b"shared_secret")
        mock_subs["crypto"].encrypt_aead.return_value = (
            b"\x00" * 12 + b"payload" + b"\x01" * 16
        )
        mock_subs["contacts"].record_message.return_value = FakeContact(
            contact_id="bob", display_name="Bob"
        )
        return app

    async def test_send_first_message_rekeys(self, send_app, mock_subs):
        result = await send_app.send_message("bob", "Hello!")
        assert result is True
        mock_subs["crypto"].kem_encapsulate.assert_called_once()
        mock_subs["inventory"].consume_key.assert_called_once_with("bob", "coin-42")
        mock_subs["network"].send_parcel.assert_awaited_once()
        mock_subs["contacts"].record_message.assert_called_once_with("bob")

    async def test_send_existing_session_no_rekey(self, send_app, mock_subs):
        """When ratchet exists and doesn't need rekey, skip KEM."""
        ratchet = MagicMock()
        ratchet.needs_rekey.return_value = False
        ratchet.derive_message_key.return_value = b"\x00" * 32
        ratchet.contact_id = "bob"
        send_app.active_sessions["bob"] = ratchet

        result = await send_app.send_message("bob", "Hey again")
        assert result is True
        mock_subs["crypto"].kem_encapsulate.assert_not_called()
        mock_subs["inventory"].select_coin.assert_not_called()

    async def test_send_no_contact_returns_false(self, app, mock_subs):
        mock_subs["contacts"].get_contact.return_value = None
        mock_subs["context"].select_coin.return_value = "GOLD"
        result = await app.send_message("unknown", "Hi")
        assert result is False

    async def test_send_no_coins_returns_false(self, send_app, mock_subs):
        mock_subs["inventory"].select_coin.return_value = None
        result = await send_app.send_message("bob", "Hi")
        assert result is False

    async def test_send_caps_tier_to_contact_ceiling(self, send_app, mock_subs):
        """STRANGER ceiling is BRONZE — GOLD ideal should be capped."""
        mock_subs["contacts"].get_contact.return_value = FakeContact(
            contact_id="bob", display_name="Bob", priority="STRANGER"
        )
        mock_subs["context"].select_coin.return_value = "GOLD"
        mock_subs["inventory"].select_coin.return_value = FakeInventoryEntry(
            contact_id="bob", key_id="c1", coin_category="BRONZE",
            public_key=b"pk", signature=b"s",
        )
        await send_app.send_message("bob", "Hi")
        # select_coin should be called with BRONZE (capped), not GOLD
        mock_subs["inventory"].select_coin.assert_called_with("bob", "BRONZE")

    async def test_send_saves_ratchet(self, send_app, mock_subs):
        await send_app.send_message("bob", "Hello!")
        mock_subs["session_store"].save_ratchet.assert_called_once()
        assert "bob" in send_app.active_sessions


# ── Receive Message ─────────────────────────────────────────────────


class TestReceiveMessage:
    def _make_parcel(self, *, with_kem=True):
        import base64
        from AQM_Database.aqm_network.protocol import frame_message

        payload = {
            "sender_id": "bob",
            "recipient_id": "alice",
            "encrypted_payload": base64.b64encode(b"enc_data").decode(),
            "aad": base64.b64encode(b"bob:alice").decode(),
        }
        if with_kem:
            payload["coin_id"] = "coin-99"
            payload["coin_tier"] = "GOLD"
            payload["kem_ciphertext"] = base64.b64encode(b"kem_ct").decode()
        return frame_message("PARCEL", payload)

    async def test_receive_with_kem_creates_session(self, app, mock_subs):
        mock_subs["vault"].fetch_key.return_value = FakeVaultEntry(
            key_id="coin-99", coin_category="GOLD",
            encrypted_blob=b"secret_key_material",
            encryption_iv=b"\x00" * 12, auth_tag=b"\x00" * 16,
        )
        mock_subs["crypto"].kem_decapsulate.return_value = b"shared_secret"
        mock_subs["crypto"].decrypt_aead.return_value = b"Hello from Bob!"
        mock_subs["contacts"].record_message.return_value = FakeContact(
            contact_id="bob", display_name="Bob"
        )

        raw = self._make_parcel(with_kem=True)
        result = await app.receive_message(raw)

        assert result == "Hello from Bob!"
        mock_subs["vault"].fetch_key.assert_called_once_with("coin-99")
        mock_subs["vault"].burn_key.assert_called_once_with("coin-99")
        mock_subs["crypto"].kem_decapsulate.assert_called_once()
        mock_subs["contacts"].record_message.assert_called_once_with("bob")
        assert "bob" in app.active_sessions

    async def test_receive_existing_session_no_kem(self, app, mock_subs):
        ratchet = MagicMock()
        ratchet.derive_message_key.return_value = b"\x00" * 32
        ratchet.contact_id = "bob"
        app.active_sessions["bob"] = ratchet

        mock_subs["crypto"].decrypt_aead.return_value = b"Follow up"
        mock_subs["contacts"].record_message.return_value = FakeContact(
            contact_id="bob", display_name="Bob"
        )

        raw = self._make_parcel(with_kem=False)
        result = await app.receive_message(raw)

        assert result == "Follow up"
        mock_subs["vault"].fetch_key.assert_not_called()

    async def test_receive_no_session_no_kem_returns_none(self, app, mock_subs):
        raw = self._make_parcel(with_kem=False)
        result = await app.receive_message(raw)
        assert result is None

    async def test_receive_vault_miss_returns_none(self, app, mock_subs):
        mock_subs["vault"].fetch_key.return_value = None
        raw = self._make_parcel(with_kem=True)
        result = await app.receive_message(raw)
        assert result is None

    async def test_receive_non_parcel_type_returns_none(self, app, mock_subs):
        from AQM_Database.aqm_network.protocol import frame_message
        raw = frame_message("ACK", {"status": "ok"})
        result = await app.receive_message(raw)
        assert result is None


# ── Ratchet helpers ─────────────────────────────────────────────────


class TestRatchetHelpers:
    def test_get_ratchet_from_cache(self, app):
        ratchet = MagicMock()
        app.active_sessions["bob"] = ratchet
        assert app._get_ratchet("bob") is ratchet

    def test_get_ratchet_from_store(self, app, mock_subs):
        ratchet = MagicMock()
        mock_subs["session_store"].load_ratchet.return_value = ratchet
        result = app._get_ratchet("bob")
        assert result is ratchet
        assert app.active_sessions["bob"] is ratchet

    def test_get_ratchet_returns_none(self, app, mock_subs):
        mock_subs["session_store"].load_ratchet.return_value = None
        assert app._get_ratchet("nobody") is None

    def test_save_ratchet_caches_and_persists(self, app, mock_subs):
        ratchet = MagicMock()
        ratchet.contact_id = "bob"
        app._save_ratchet(ratchet)
        assert app.active_sessions["bob"] is ratchet
        mock_subs["session_store"].save_ratchet.assert_called_once_with(ratchet)


# ── Cap Tier ────────────────────────────────────────────────────────


class TestCapTier:
    def test_gold_capped_to_silver(self):
        assert _cap_tier("GOLD", "SILVER") == "SILVER"

    def test_gold_capped_to_bronze(self):
        assert _cap_tier("GOLD", "BRONZE") == "BRONZE"

    def test_silver_under_gold_ceiling(self):
        assert _cap_tier("SILVER", "GOLD") == "SILVER"

    def test_same_tier(self):
        assert _cap_tier("BRONZE", "BRONZE") == "BRONZE"

    def test_bronze_under_any_ceiling(self):
        assert _cap_tier("BRONZE", "GOLD") == "BRONZE"


# ── Shutdown ────────────────────────────────────────────────────────


class TestShutdown:
    async def test_shutdown_disconnects_network(self, app, mock_subs):
        await app.shutdown()
        mock_subs["network"].disconnect.assert_awaited_once()
