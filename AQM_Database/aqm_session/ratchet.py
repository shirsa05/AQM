import struct
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

def _hkdf_derive(key_material: bytes, info: bytes, length: int = 32) -> bytes:

    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info,
    ).derive(key_material)


class SessionRatchet:
    """
    Implements the Amortized Quantum Messaging (AQM) Session Ratchet.
    Derives symmetric AES-256 keys from a single Post-Quantum master secret.

    Uses SEPARATE send and receive chains so each side's counter is independent.
    The initiator (who did KEM encapsulate) uses chain-A for sending and chain-B
    for receiving. The responder (who did KEM decapsulate) uses chain-B for
    sending and chain-A for receiving. This ensures both sides derive matching
    keys while keeping counters individual.

    Rekey is triggered when the SEND counter reaches the tier limit.
    """

    # Dynamic Tier-Based Limits for Amortization
    TIER_LIMITS = {
        "GOLD": 25,
        "SILVER": 10,
        "BRONZE": 5
    }

    def __init__(self, contact_id: str, coin_tier: str, master_secret: bytes = None,
                 is_initiator: bool = True):

        self.contact_id = contact_id

        # Validate and assign the tier limit
        if coin_tier not in self.TIER_LIMITS:
            raise ValueError(f"Invalid coin tier: {coin_tier}. Must be GOLD, SILVER, or BRONZE.")

        self.coin_tier = coin_tier
        self.max_messages = self.TIER_LIMITS[self.coin_tier]
        self.is_initiator = is_initiator

        self.send_counter = 0
        self.recv_counter = 0
        self.has_sent_first = is_initiator
        self.send_chain_key = None
        self.recv_chain_key = None

        # Legacy compat: msg_counter points to send_counter for display

        if master_secret:
            # Derive two independent chains from the same master secret
            chain_a = _hkdf_derive(master_secret, info=b"aqm-chain-A-init")
            chain_b = _hkdf_derive(master_secret, info=b"aqm-chain-B-init")

            if is_initiator:
                self.send_chain_key = chain_a
                self.recv_chain_key = chain_b
            else:
                self.send_chain_key = chain_b
                self.recv_chain_key = chain_a

    @property
    def msg_counter(self):
        """Send counter — used for display and rekey threshold."""
        return self.send_counter

    @property
    def current_chain_key(self):
        """Legacy compat — returns send chain key."""
        return self.send_chain_key

    def derive_message_key(self) -> bytes:
        """Derive next send key. Use derive_send_key() or derive_recv_key() instead."""
        return self.derive_send_key()

    def derive_send_key(self) -> bytes:
        """Derive next AES-256 key for sending. Increments send counter only."""
        if self.needs_rekey():
            raise ValueError(
                f"Ratchet exhausted for {self.contact_id} "
                f"({self.coin_tier} limit: {self.max_messages} reached). Must rekey."
            )

        counter_bytes = struct.pack(">Q", self.send_counter)

        message_key = _hkdf_derive(
            self.send_chain_key,
            info=b"aqm-msg-" + counter_bytes
        )

        self.send_chain_key = _hkdf_derive(
            self.send_chain_key,
            info=b"aqm-chain-advance"
        )

        self.send_counter += 1
        self.has_sent_first = True

        return message_key

    def derive_recv_key(self) -> bytes:
        """Derive next AES-256 key for receiving. Increments recv counter only."""
        counter_bytes = struct.pack(">Q", self.recv_counter)

        message_key = _hkdf_derive(
            self.recv_chain_key,
            info=b"aqm-msg-" + counter_bytes
        )

        self.recv_chain_key = _hkdf_derive(
            self.recv_chain_key,
            info=b"aqm-chain-advance"
        )

        self.recv_counter += 1

        return message_key

    def needs_rekey(self) -> bool:
        if not self.is_initiator and not self.has_sent_first:
            return True
        return self.send_counter >= self.max_messages

    def rekey(self, new_master_secret: bytes, new_coin_tier: str,
              is_initiator: bool = True) -> None:

        if new_coin_tier not in self.TIER_LIMITS:
            raise ValueError(f"Invalid coin tier: {new_coin_tier}. Must be GOLD, SILVER, or BRONZE.")

        # Update tier and dynamically adjust the limit
        self.coin_tier = new_coin_tier
        self.max_messages = self.TIER_LIMITS[self.coin_tier]
        self.is_initiator = is_initiator

        # Reset both chains and counters
        self.send_counter = 0
        self.recv_counter = 0
        self.has_sent_first = is_initiator
        
        chain_a = _hkdf_derive(new_master_secret, info=b"aqm-chain-A-init")
        chain_b = _hkdf_derive(new_master_secret, info=b"aqm-chain-B-init")

        if is_initiator:
            self.send_chain_key = chain_a
            self.recv_chain_key = chain_b
        else:
            self.send_chain_key = chain_b
            self.recv_chain_key = chain_a

    def rekey_recv_only(self, new_master_secret: bytes, new_coin_tier: str,
                    is_initiator: bool = False) -> None:
        """
        Reinitialise only the receive chain from a new master secret.
        Called when a rekey parcel is RECEIVED — the other side started a new
        session, so we need a new recv chain, but our send chain is unaffected.
        """
        if new_coin_tier not in self.TIER_LIMITS:
            raise ValueError(f"Invalid coin tier: {new_coin_tier}.")

        self.coin_tier     = new_coin_tier
        self.max_messages  = self.TIER_LIMITS[new_coin_tier]
        self.is_initiator  = is_initiator
        self.recv_counter  = 0
        # Do NOT touch send_counter or has_sent_first

        chain_a = _hkdf_derive(new_master_secret, info=b"aqm-chain-A-init")
        chain_b = _hkdf_derive(new_master_secret, info=b"aqm-chain-B-init")

        # Responder receives on chain-A (initiator sends on chain-A)
        # Initiator receives on chain-B (responder sends on chain-B)
        if is_initiator:
            self.recv_chain_key = chain_b
        else:
            self.recv_chain_key = chain_a
        
    def get_state(self) -> dict:

        if not self.send_chain_key:
            raise ValueError("Ratchet is not initialized.")

        return {
            "contact_id": self.contact_id,
            "coin_tier": self.coin_tier,
            "send_counter": self.send_counter,
            "recv_counter": self.recv_counter,
            "send_chain_key": self.send_chain_key.hex(),
            "recv_chain_key": self.recv_chain_key.hex(),
            "is_initiator": self.is_initiator,
            "has_sent_first": self.has_sent_first,
            # Legacy compat
            "msg_counter": self.send_counter,
            "current_chain_key": self.send_chain_key.hex(),
        }

    @classmethod
    def from_state(cls, state: dict) -> 'SessionRatchet':

        ratchet = cls(contact_id=state["contact_id"], coin_tier=state["coin_tier"])

        # Support both new and legacy state formats
        if "send_chain_key" in state:
            ratchet.send_counter = state["send_counter"]
            ratchet.recv_counter = state["recv_counter"]
            ratchet.send_chain_key = bytes.fromhex(state["send_chain_key"])
            ratchet.recv_chain_key = bytes.fromhex(state["recv_chain_key"])
            ratchet.is_initiator = state.get("is_initiator", True)
            ratchet.has_sent_first = state.get("has_sent_first", ratchet.send_counter > 0)
        else:
            # Legacy format migration: single chain becomes send chain,
            # recv chain initialized as copy (will resync on next rekey)
            ratchet.send_counter = state["msg_counter"]
            ratchet.recv_counter = state.get("recv_counter", 0)
            ratchet.send_chain_key = bytes.fromhex(state["current_chain_key"])
            ratchet.recv_chain_key = bytes.fromhex(state["current_chain_key"])
            ratchet.is_initiator = state.get("is_initiator", True)
            ratchet.has_sent_first = state.get("has_sent_first", ratchet.send_counter > 0)

        return ratchet
