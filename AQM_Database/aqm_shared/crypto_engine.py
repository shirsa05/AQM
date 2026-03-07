"""
Post-quantum crypto engine for AQM.

Requires liboqs-python + pynacl. No fallbacks. No mocks.
If either dependency is missing, ImportError crashes immediately.
"""

import os

import uuid
from dataclasses import dataclass

import oqs                # Kyber-768 KEM + Dilithium-3 — REQUIRED
import nacl.signing       # Ed25519 — REQUIRED
import nacl.public        # X25519 — REQUIRED
from nacl.exceptions import BadSignatureError
import nacl.bindings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError

# ─── Key sizes (bytes) ───

KYBER768_PK_SIZE = 1184
KYBER768_SK_SIZE = 2400
X25519_PK_SIZE = 32
X25519_SK_SIZE = 32
ED25519_SIG_SIZE = 64

@dataclass
class MintedCoinBundle:
    """All artifacts produced by minting a single coin."""
    key_id: str
    coin_category: str
    public_key: bytes
    secret_key: bytes
    signature: bytes
    signing_public_key: bytes | None = None   # Dilithium pk (GOLD only)


def generate_keypair_gold_silver() -> tuple[bytes, bytes]:
    with oqs.KeyEncapsulation("ML-KEM-768") as kem:
        public_key = kem.generate_keypair()
        secret_key = kem.export_secret_key()
        return bytes(public_key), bytes(secret_key)


class CryptoEngine:
    """Key generation and signing. Real crypto only."""

    def __init__(self):
        self._signing_key = nacl.signing.SigningKey.generate()

    def generate_keypair_bronze(self) -> tuple[bytes, bytes]:
        private_key = nacl.public.PrivateKey.generate()
        return bytes(private_key.public_key), bytes(private_key)

    def sign_dilithium(self , data:bytes , signing_key : bytes) -> bytes:
        sig = oqs.Signature("ML-DSA-65")
        sig.secret_key = signing_key
        signature = sig.sign(data)
        return signature

    def verify_dilithium(self , data:bytes , signature : bytes , public_key : bytes) -> bool:
        with oqs.Signature("ML-DSA-65") as sig:
            return sig.verify(data, signature , public_key=public_key)

    def sign_ed25519(self , data:bytes , signing_key : nacl.signing.SigningKey) -> bytes:
        return signing_key.sign(data).signature

    def verify_ed25519(self , data:bytes , signature : bytes , public_key : bytes) -> bool:
        verify_key = nacl.signing.VerifyKey(public_key)
        try :
            verify_key.verify(data , signature)
            return True
        except BadSignatureError:
            return False

    # AFTER
    def kem_encapsulate(self, public_key: bytes, tier: str = "GOLD") -> tuple[bytes, bytes]:
        if tier == "BRONZE":
            # X25519 ECDH — generate ephemeral keypair, send ephemeral pubkey as ciphertext
            ephemeral_sk  = nacl.public.PrivateKey.generate()
            peer_pk       = nacl.public.PublicKey(public_key)
            shared_secret = nacl.bindings.crypto_scalarmult(bytes(ephemeral_sk), bytes(peer_pk))
            ciphertext    = bytes(ephemeral_sk.public_key)   # 32 bytes
            return ciphertext, shared_secret
        else:
            with oqs.KeyEncapsulation("ML-KEM-768") as client:
                ciphertext, shared_secret = client.encap_secret(public_key)
                return ciphertext, shared_secret
            
    # AFTER
    def kem_decapsulate(self, ciphertext: bytes, secret_key: bytes, tier: str = "GOLD") -> bytes:
        if tier == "BRONZE":
            # X25519 ECDH — ciphertext is sender's ephemeral pubkey
            shared_secret = nacl.bindings.crypto_scalarmult(secret_key, ciphertext)
            return shared_secret
        else:
            server = oqs.KeyEncapsulation("ML-KEM-768")
            server.secret_key = secret_key
            shared_secret = server.decap_secret(ciphertext)
            return shared_secret

    def dh_exchange(self , my_secret : bytes , their_public : bytes) -> bytes:
        shared_secret = nacl.bindings.crypto_scalarmult(my_secret, their_public)
        return shared_secret

    def encrypt_aead(self , plaintext : bytes , key : bytes , aad:bytes) -> bytes:
        """
        AES-256-GCM encryption.
        Returns: nonce (12 bytes) || ciphertext || tag (16 bytes)
        """
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ct_tag = aesgcm.encrypt(nonce, plaintext , aad)
        return nonce + ct_tag

    def decrypt_aead(self , ciphertext : bytes , key : bytes , aad:bytes) -> bytes:
        """
        AES-256-GCM decryption.
        Input: nonce (12 bytes) || ciphertext || tag (16 bytes)
        Returns: plaintext
        """
        nonce = ciphertext[:12]
        ct_tag = ciphertext[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ct_tag , aad)
        return plaintext


    def mint_coin(self , coin_category:str) -> MintedCoinBundle:
        if coin_category not in config.VALID_COIN_CATEGORIES:
            raise InvalidCoinCategoryError(coin_category)

        key_id = str(uuid.uuid4())
        signing_public_key = None

        if coin_category == "GOLD":
            pk , sk = generate_keypair_gold_silver()
            with oqs.Signature("ML-DSA-65") as signer:
                signing_public_key = bytes(signer.generate_keypair())
                dil_sk = signer.export_secret_key()
            sig = self.sign_dilithium(pk, dil_sk)
        elif coin_category == "SILVER":
            pk , sk = generate_keypair_gold_silver()
            sig = self.sign_ed25519(pk, self._signing_key)
        else:
            pk , sk = self.generate_keypair_bronze()
            sig = self.sign_ed25519(pk, self._signing_key)

        return MintedCoinBundle(
            key_id=key_id,
            coin_category=coin_category,
            public_key=pk,
            secret_key=sk,
            signature=sig,
            signing_public_key=signing_public_key,
        )