import asyncio
import logging
import os
from typing import Optional, Dict

from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_session.session_store import SessionStore
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_network.client import Client
from AQM_Database.aqm_network.protocol import frame_message, parse_message
from AQM_Database.aqm_shared.context_manager import ContextManager, DeviceContext
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_shared.config import BUDGET_CAPS, TIER_CEILING

logger = logging.getLogger("aqm.app")


class AQMApp:
    """Main application class. Owns all subsystems, orchestrates the AQM lifecycle."""

    def __init__(
        self,
        user_id: str,
        server_url: str,
        *,
        vault: Optional[SecureVault] = None,
        inventory: Optional[SmartInventory] = None,
        contacts: Optional[ContactsDatabase] = None,
        session_store: Optional[SessionStore] = None,
        network: Optional[Client] = None,
        crypto: Optional[CryptoEngine] = None,
        context: Optional[ContextManager] = None,
    ):
        self.user_id = user_id

        # Subsystems — accept injected deps for testing, else create real ones
        self.vault = vault or SecureVault(create_vault_client())
        self.inventory = inventory or SmartInventory(create_inventory_client())
        self.contacts = contacts or ContactsDatabase()
        self.session_store = session_store or SessionStore()
        self.network = network or Client(server_url, user_id)
        self.crypto = crypto or CryptoEngine()
        self.context = context or ContextManager()

        # In-memory cache for active session ratchets
        self.active_sessions: Dict[str, SessionRatchet] = {}

    # ── Boot ────────────────────────────────────────────────────────

    async def start(self):
        """Boot sequence: register message callback, connect network, mint coins."""
        self.network.on_message(self._on_network_message)
        await self.network.connect()
        await self.mint_coins()
        logger.info("AQM App online for %s", self.user_id)

    # ── Minting ─────────────────────────────────────────────────────

    async def mint_coins(self):
        """
        Mint coins per BUDGET_CAPS["BESTIE"] targets (5G + 4S + 1B).
        Private keys go to vault (encrypted). Public keys are for uploading to server.
        Returns list of (key_id, coin_category, public_key, signature, signing_public_key)
        for the caller / bridge to upload.
        """
        targets = BUDGET_CAPS["BESTIE"]  # {GOLD: 5, SILVER: 4, BRONZE: 1}
        minted = []

        for tier, count in targets.items():
            current = self.vault.count_active(tier)
            # count_active returns int when coin_category is specified
            needed = count - current

            for _ in range(max(0, needed)):
                bundle = self.crypto.mint_coin(tier)

                # Encrypt the secret_key with AEAD before vault storage.
                # Use a random 32-byte vault encryption key (in production this
                # would come from a device master key; here we derive one).
                vault_key = os.urandom(32)
                aad = bundle.key_id.encode()
                blob = self.crypto.encrypt_aead(bundle.secret_key, vault_key, aad)

                # encrypt_aead returns nonce(12) || ciphertext || tag(16)
                iv = blob[:12]
                auth_tag = blob[-16:]
                encrypted_blob = blob[12:-16]

                self.vault.store_key(
                    key_id=bundle.key_id,
                    coin_category=bundle.coin_category,
                    encrypted_blob=encrypted_blob,
                    encryption_iv=iv,
                    auth_tag=auth_tag,
                )

                minted.append(bundle)
                logger.debug("Minted %s coin %s", tier, bundle.key_id)

        logger.info(
            "Minting complete: %d new coins (%s)",
            len(minted),
            ", ".join(f"{t}={targets[t]}" for t in targets),
        )
        return minted

    # ── Contacts ────────────────────────────────────────────────────

    async def add_contact(self, contact_id: str, display_name: str, signing_key: bytes = None):
        """Add contact to DB and register in inventory."""
        contact = self.contacts.add_contact(contact_id, display_name, signing_key)

        # Register in inventory so we can store/select their public coins
        self.inventory.register_contact(
            contact_id, contact.priority, display_name
        )

        # Provision: fetch their public keys from server based on priority budget
        await self.provision_contact(contact_id, contact.priority)
        return contact

    async def provision_contact(self, contact_id: str, priority: str):
        """
        Fetch public keys from server for this contact based on priority budget.
        In production this calls bridge.fetch_and_cache(). Stubbed for now.
        """
        logger.info("Provisioning keys for %s (priority=%s)", contact_id, priority)

    # ── Send ────────────────────────────────────────────────────────

    async def send_message(
        self, contact_id: str, plaintext: str, device_ctx: Optional[DeviceContext] = None
    ) -> bool:
        """
        Full send flow:
        1. Device context → select tier
        2. Get/create SessionRatchet
        3. If needs rekey: consume coin from inventory, run KEM, init/rekey ratchet
        4. Derive message key
        5. Encrypt with AEAD
        6. Frame as PARCEL
        7. Send via network
        8. Record message in contacts DB
        """
        try:
            # 1. Tier selection from device context
            if device_ctx is None:
                device_ctx = DeviceContext(
                    battery_pct=100.0, wifi_connected=True, signal_dbm=-50.0
                )
            ideal_tier = self.context.select_coin(device_ctx)

            # Cap tier by contact priority ceiling
            contact = self.contacts.get_contact(contact_id)
            if contact is None:
                logger.error("Contact %s not found", contact_id)
                return False

            ceiling = TIER_CEILING.get(contact.priority, "BRONZE")
            tier = _cap_tier(ideal_tier, ceiling)

            # 2. Get or create ratchet
            ratchet = self._get_ratchet(contact_id)

            coin_id = None
            kem_ciphertext = None
            coin_tier = None

            # 3. Rekey if needed (or first message)
            if ratchet is None or ratchet.needs_rekey():
                coin = self.inventory.select_coin(contact_id, tier)
                if coin is None:
                    logger.error("No %s coins available for %s", tier, contact_id)
                    return False

                # KEM encapsulate with their public key
                ct, shared_secret = self.crypto.kem_encapsulate(coin.public_key)
                kem_ciphertext = ct

                if ratchet is None:
                    ratchet = SessionRatchet(contact_id, coin.coin_category, shared_secret)
                else:
                    ratchet.rekey(shared_secret, coin.coin_category)

                coin_id = coin.key_id
                coin_tier = coin.coin_category

                # Consume the used coin
                self.inventory.consume_key(contact_id, coin.key_id)

            # 4. Derive message key
            message_key = ratchet.derive_message_key()

            # 5. Encrypt
            aad = f"{self.user_id}:{contact_id}".encode()
            encrypted_payload = self.crypto.encrypt_aead(
                plaintext.encode(), message_key, aad
            )

            # 6. Frame as PARCEL
            import base64

            parcel_payload = {
                "sender_id": self.user_id,
                "recipient_id": contact_id,
                "encrypted_payload": base64.b64encode(encrypted_payload).decode(),
                "aad": base64.b64encode(aad).decode(),
            }
            if coin_id is not None:
                parcel_payload["coin_id"] = coin_id
                parcel_payload["coin_tier"] = coin_tier
                parcel_payload["kem_ciphertext"] = base64.b64encode(kem_ciphertext).decode()

            framed = frame_message("PARCEL", parcel_payload)

            # 7. Send
            await self.network.send_parcel(contact_id, framed.encode())

            # 8. Record
            self.contacts.record_message(contact_id)
            self._save_ratchet(ratchet)

            logger.info("Sent message to %s (tier=%s, rekey=%s)", contact_id, tier, coin_id is not None)
            return True

        except Exception as e:
            logger.error("Send failed: %s", e)
            return False

    # ── Receive ─────────────────────────────────────────────────────

    async def receive_message(self, raw_parcel: str) -> Optional[str]:
        """
        Full receive flow:
        1. Parse parcel
        2. If KEM ciphertext present: fetch private key from vault, decapsulate, init/rekey ratchet
        3. Derive message key
        4. Decrypt payload
        5. Burn vault key if KEM exchange happened
        6. Record message
        Returns plaintext string or None on failure.
        """
        try:
            import base64

            msg_type, payload = parse_message(raw_parcel)
            if msg_type != "PARCEL":
                logger.warning("Unexpected message type: %s", msg_type)
                return None

            sender_id = payload["sender_id"]
            encrypted_payload = base64.b64decode(payload["encrypted_payload"])
            aad = base64.b64decode(payload.get("aad", ""))

            ratchet = self._get_ratchet(sender_id)

            # KEM decapsulation (new session or rekey)
            if "kem_ciphertext" in payload:
                coin_id = payload["coin_id"]
                kem_ct = base64.b64decode(payload["kem_ciphertext"])
                coin_tier = payload["coin_tier"]

                # Fetch our private key from vault
                entry = self.vault.fetch_key(coin_id)
                if entry is None:
                    logger.error("Vault key %s not found", coin_id)
                    return None

                # Decapsulate KEM using the stored secret key material.
                # In production the encrypted_blob would be decrypted with the
                # device master key first. For MVP we treat it as the raw secret key.
                shared_secret = self.crypto.kem_decapsulate(kem_ct, entry.encrypted_blob)

                if ratchet is None:
                    ratchet = SessionRatchet(sender_id, coin_tier, shared_secret)
                else:
                    ratchet.rekey(shared_secret, coin_tier)

                # Burn the one-time private key
                self.vault.burn_key(coin_id)

            if ratchet is None:
                logger.error("No session with %s and no KEM data in parcel", sender_id)
                return None

            # Derive message key
            message_key = ratchet.derive_message_key()

            # Decrypt
            plaintext_bytes = self.crypto.decrypt_aead(encrypted_payload, message_key, aad)
            plaintext = plaintext_bytes.decode()

            # Record + save
            self.contacts.record_message(sender_id)
            self._save_ratchet(ratchet)

            logger.info("Received message from %s", sender_id)
            return plaintext

        except Exception as e:
            logger.error("Receive failed: %s", e)
            return None

    # ── Network callback ────────────────────────────────────────────

    def _on_network_message(self, raw_parcel: str):
        """Callback registered with network.on_message(). Dispatches to async receive."""
        asyncio.ensure_future(self.receive_message(raw_parcel))

    # ── Session helpers ─────────────────────────────────────────────

    def _get_ratchet(self, contact_id: str) -> Optional[SessionRatchet]:
        if contact_id in self.active_sessions:
            return self.active_sessions[contact_id]
        ratchet = self.session_store.load_ratchet(contact_id)
        if ratchet is not None:
            self.active_sessions[contact_id] = ratchet
        return ratchet

    def _save_ratchet(self, ratchet: SessionRatchet):
        self.active_sessions[ratchet.contact_id] = ratchet
        self.session_store.save_ratchet(ratchet)

    # ── Shutdown ────────────────────────────────────────────────────

    async def shutdown(self):
        """Graceful shutdown."""
        await self.network.disconnect()
        logger.info("AQM App shut down for %s", self.user_id)


# ── Helpers ─────────────────────────────────────────────────────────

_TIER_RANK = {"GOLD": 2, "SILVER": 1, "BRONZE": 0}


def _cap_tier(ideal: str, ceiling: str) -> str:
    """Cap the ideal tier to the ceiling. E.g. ideal=GOLD, ceiling=SILVER → SILVER."""
    if _TIER_RANK.get(ideal, 0) > _TIER_RANK.get(ceiling, 0):
        return ceiling
    return ideal
