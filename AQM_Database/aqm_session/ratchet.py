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
    The amortization window depends dynamically on the Coin Tier used.
    """
    
    # Dynamic Tier-Based Limits for Amortization
    TIER_LIMITS = {
        "GOLD": 250,
        "SILVER": 150,
        "BRONZE": 75
    }

    def __init__(self, contact_id: str, coin_tier: str, master_secret: bytes = None):
       
        self.contact_id = contact_id
        
        # Validate and assign the tier limit
        if coin_tier not in self.TIER_LIMITS:
            raise ValueError(f"Invalid coin tier: {coin_tier}. Must be GOLD, SILVER, or BRONZE.")
            
        self.coin_tier = coin_tier
        self.max_messages = self.TIER_LIMITS[self.coin_tier]
        
        self.msg_counter = 0
        self.current_chain_key = None
        
        if master_secret:
            # Initialize the root of the chain
            self.current_chain_key = _hkdf_derive(master_secret, info=b"aqm-chain-init")

    def derive_message_key(self) -> bytes:
       
        if self.needs_rekey():
            raise ValueError(
                f"Ratchet exhausted for {self.contact_id} "
                f"({self.coin_tier} limit: {self.max_messages} reached). Must rekey."
            )

        
        counter_bytes = struct.pack(">Q", self.msg_counter)
        
        
        message_key = _hkdf_derive(
            self.current_chain_key, 
            info=b"aqm-msg-" + counter_bytes
        )
        
        
        self.current_chain_key = _hkdf_derive(
            self.current_chain_key, 
            info=b"aqm-chain-advance"
        )
        
        
        self.msg_counter += 1
        
        return message_key

    def needs_rekey(self) -> bool:
        
        return self.msg_counter >= self.max_messages

    def rekey(self, new_master_secret: bytes, new_coin_tier: str) -> None:
        
        if new_coin_tier not in self.TIER_LIMITS:
            raise ValueError(f"Invalid coin tier: {new_coin_tier}. Must be GOLD, SILVER, or BRONZE.")
            
        # Update tier and dynamically adjust the limit
        self.coin_tier = new_coin_tier
        self.max_messages = self.TIER_LIMITS[self.coin_tier]
        
        # Reset chain and counter
        self.msg_counter = 0
        self.current_chain_key = _hkdf_derive(new_master_secret, info=b"aqm-chain-init")

    def get_state(self) -> dict:
       
        if not self.current_chain_key:
            raise ValueError("Ratchet is not initialized.")
            
        return {
            "contact_id": self.contact_id,
            "coin_tier": self.coin_tier,
            "msg_counter": self.msg_counter,
            "current_chain_key": self.current_chain_key.hex()
        }

    @classmethod
    def from_state(cls, state: dict) -> 'SessionRatchet':
        
        # Rebuild the class instance without needing the master secret
        ratchet = cls(contact_id=state["contact_id"], coin_tier=state["coin_tier"])
        ratchet.msg_counter = state["msg_counter"]
        ratchet.current_chain_key = bytes.fromhex(state["current_chain_key"])
        return ratchet
