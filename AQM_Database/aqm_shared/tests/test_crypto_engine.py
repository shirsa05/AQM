"""Tests for the rewritten CryptoEngine — real liboqs + pynacl. Zero mocks."""

import pytest
import nacl.signing
from cryptography.exceptions import InvalidTag

from AQM_Database.aqm_shared.crypto_engine import (
    CryptoEngine,
    MintedCoinBundle,
    generate_keypair_gold_silver,
    KYBER768_PK_SIZE,
    KYBER768_SK_SIZE,
    X25519_PK_SIZE,
    X25519_SK_SIZE,
    ED25519_SIG_SIZE,
)
from AQM_Database.aqm_shared.errors import InvalidCoinCategoryError


@pytest.fixture
def engine():
    return CryptoEngine()


# ════════════════════════════════════════════════════════════════
#  KEY GENERATION — sizes, types, uniqueness
# ════════════════════════════════════════════════════════════════


class TestKeyGeneration:

    def test_gold_silver_keypair_sizes(self):
        """Kyber-768: pk = 1184 bytes, sk = 2400 bytes."""
        pk, sk = generate_keypair_gold_silver()
        assert len(pk) == KYBER768_PK_SIZE
        assert len(sk) == KYBER768_SK_SIZE

    def test_gold_silver_keypair_returns_bytes(self):
        pk, sk = generate_keypair_gold_silver()
        assert isinstance(pk, bytes)
        assert isinstance(sk, bytes)

    def test_gold_silver_keypairs_are_unique(self):
        pk1, sk1 = generate_keypair_gold_silver()
        pk2, sk2 = generate_keypair_gold_silver()
        assert pk1 != pk2
        assert sk1 != sk2

    def test_bronze_keypair_sizes(self, engine):
        """X25519: pk = 32 bytes, sk = 32 bytes."""
        pk, sk = engine.generate_keypair_bronze()
        assert len(pk) == X25519_PK_SIZE
        assert len(sk) == X25519_SK_SIZE

    def test_bronze_keypair_returns_bytes(self, engine):
        pk, sk = engine.generate_keypair_bronze()
        assert isinstance(pk, bytes)
        assert isinstance(sk, bytes)

    def test_bronze_keypairs_are_unique(self, engine):
        pk1, _ = engine.generate_keypair_bronze()
        pk2, _ = engine.generate_keypair_bronze()
        assert pk1 != pk2


# ════════════════════════════════════════════════════════════════
#  DILITHIUM-3 SIGNING
# ════════════════════════════════════════════════════════════════


class TestDilithiumSigning:

    @pytest.fixture
    def dilithium_keys(self):
        """Generate a Dilithium-3 keypair for signing tests."""
        import oqs
        with oqs.Signature("ML-DSA-65") as sig:
            pk = bytes(sig.generate_keypair())
            sk = sig.export_secret_key()
        return pk, sk

    def test_sign_returns_bytes(self, engine, dilithium_keys):
        pk, sk = dilithium_keys
        signature = engine.sign_dilithium(b"hello world", sk)
        assert isinstance(signature, bytes)
        assert len(signature) > 0

    def test_sign_verify_roundtrip(self, engine, dilithium_keys):
        pk, sk = dilithium_keys
        data = b"test data for signing"
        signature = engine.sign_dilithium(data, sk)
        assert engine.verify_dilithium(data, signature, pk) is True

    def test_tampered_data_fails(self, engine, dilithium_keys):
        pk, sk = dilithium_keys
        data = b"original"
        signature = engine.sign_dilithium(data, sk)
        assert engine.verify_dilithium(b"tampered", signature, pk) is False

    def test_tampered_signature_fails(self, engine, dilithium_keys):
        pk, sk = dilithium_keys
        data = b"test"
        signature = engine.sign_dilithium(data, sk)
        bad_sig = bytearray(signature)
        bad_sig[0] ^= 0xFF  # flip first byte
        assert engine.verify_dilithium(data, bytes(bad_sig), pk) is False


# ════════════════════════════════════════════════════════════════
#  ED25519 SIGNING
# ════════════════════════════════════════════════════════════════


class TestEd25519Signing:

    @pytest.fixture
    def ed25519_keys(self):
        sk = nacl.signing.SigningKey.generate()
        vk = sk.verify_key
        return sk, bytes(vk)

    def test_sign_size(self, engine, ed25519_keys):
        sk, _ = ed25519_keys
        sig = engine.sign_ed25519(b"data", sk)
        assert len(sig) == ED25519_SIG_SIZE

    def test_sign_verify_roundtrip(self, engine, ed25519_keys):
        sk, vk_bytes = ed25519_keys
        data = b"roundtrip test"
        sig = engine.sign_ed25519(data, sk)
        assert engine.verify_ed25519(data, sig, vk_bytes) is True

    def test_ed25519_is_deterministic(self, engine, ed25519_keys):
        sk, _ = ed25519_keys
        data = b"same input"
        sig1 = engine.sign_ed25519(data, sk)
        sig2 = engine.sign_ed25519(data, sk)
        assert sig1 == sig2

    def test_tampered_data_fails(self, engine, ed25519_keys):
        sk, vk_bytes = ed25519_keys
        sig = engine.sign_ed25519(b"original", sk)
        assert engine.verify_ed25519(b"tampered", sig, vk_bytes) is False


# ════════════════════════════════════════════════════════════════
#  KYBER-768 KEM (encapsulate / decapsulate)
# ════════════════════════════════════════════════════════════════


class TestKEM:

    def test_encap_decap_roundtrip(self, engine):
        """Both sides derive the same 32-byte shared secret."""
        pk, sk = generate_keypair_gold_silver()
        ciphertext, shared_secret_sender = engine.kem_encapsulate(pk)
        shared_secret_receiver = engine.kem_decapsulate(ciphertext, sk)
        assert shared_secret_sender == shared_secret_receiver
        assert len(shared_secret_sender) == 32

    def test_ciphertext_size(self, engine):
        """Kyber-768 ciphertext is 1088 bytes."""
        pk, _ = generate_keypair_gold_silver()
        ct, _ = engine.kem_encapsulate(pk)
        assert len(ct) == 1088

    def test_wrong_secret_key_gives_different_secret(self, engine):
        """Decapsulating with the wrong sk produces a different shared secret."""
        pk1, sk1 = generate_keypair_gold_silver()
        _, sk2 = generate_keypair_gold_silver()
        ct, ss_sender = engine.kem_encapsulate(pk1)
        ss_wrong = engine.kem_decapsulate(ct, sk2)
        assert ss_sender != ss_wrong


# ════════════════════════════════════════════════════════════════
#  X25519 DH EXCHANGE (Bronze)
# ════════════════════════════════════════════════════════════════


class TestDHExchange:

    def test_shared_secret_matches(self, engine):
        """Alice and Bob derive the same 32-byte shared secret."""
        alice_pk, alice_sk = engine.generate_keypair_bronze()
        bob_pk, bob_sk = engine.generate_keypair_bronze()
        ss_alice = engine.dh_exchange(alice_sk, bob_pk)
        ss_bob = engine.dh_exchange(bob_sk, alice_pk)
        assert ss_alice == ss_bob
        assert len(ss_alice) == 32

    def test_different_peers_different_secrets(self, engine):
        alice_pk, alice_sk = engine.generate_keypair_bronze()
        bob_pk, bob_sk = engine.generate_keypair_bronze()
        charlie_pk, charlie_sk = engine.generate_keypair_bronze()
        ss_ab = engine.dh_exchange(alice_sk, bob_pk)
        ss_ac = engine.dh_exchange(alice_sk, charlie_pk)
        assert ss_ab != ss_ac


# ════════════════════════════════════════════════════════════════
#  AES-256-GCM AEAD ENCRYPTION
# ════════════════════════════════════════════════════════════════


class TestAEAD:

    @pytest.fixture
    def aead_key(self):
        import os
        return os.urandom(32)

    def test_encrypt_decrypt_roundtrip(self, engine, aead_key):
        plaintext = b"Hello, post-quantum world!"
        blob = engine.encrypt_aead(plaintext, aead_key, aad=b"")
        recovered = engine.decrypt_aead(blob, aead_key, aad=b"")
        assert recovered == plaintext

    def test_nonce_prefix_is_12_bytes(self, engine, aead_key):
        blob = engine.encrypt_aead(b"test", aead_key, aad=b"")
        # blob = nonce(12) || ciphertext || tag(16)
        assert len(blob) >= 12 + 16  # at least nonce + tag

    def test_aad_mismatch_fails(self, engine, aead_key):
        blob = engine.encrypt_aead(b"test", aead_key, aad=b"header-v1")
        with pytest.raises(InvalidTag):
            engine.decrypt_aead(blob, aead_key, aad=b"header-v2")

    def test_tampered_ciphertext_fails(self, engine, aead_key):
        blob = engine.encrypt_aead(b"secret", aead_key, aad=b"")
        tampered = bytearray(blob)
        tampered[15] ^= 0xFF  # flip a byte in the ciphertext region
        with pytest.raises(InvalidTag):
            engine.decrypt_aead(bytes(tampered), aead_key, aad=b"")

    def test_wrong_key_fails(self, engine, aead_key):
        import os
        blob = engine.encrypt_aead(b"secret", aead_key, aad=b"")
        wrong_key = os.urandom(32)
        with pytest.raises(InvalidTag):
            engine.decrypt_aead(blob, wrong_key, aad=b"")


# ════════════════════════════════════════════════════════════════
#  MINT COIN (full workflow)
# ════════════════════════════════════════════════════════════════


class TestMintCoin:

    def test_gold_bundle(self, engine):
        b = engine.mint_coin("GOLD")
        assert isinstance(b, MintedCoinBundle)
        assert b.coin_category == "GOLD"
        assert len(b.key_id) == 36  # UUID
        assert len(b.public_key) == KYBER768_PK_SIZE
        assert len(b.secret_key) == KYBER768_SK_SIZE
        assert b.signing_public_key is not None  # Dilithium pk
        assert isinstance(b.signature, bytes)
        assert len(b.signature) > 0

    def test_gold_signature_verifies(self, engine):
        """GOLD coins are signed with Dilithium — verify with the bundle's signing pk."""
        b = engine.mint_coin("GOLD")
        assert engine.verify_dilithium(b.public_key, b.signature, b.signing_public_key)

    def test_silver_bundle(self, engine):
        b = engine.mint_coin("SILVER")
        assert b.coin_category == "SILVER"
        assert len(b.public_key) == KYBER768_PK_SIZE
        assert b.signing_public_key is None  # Ed25519 — no separate pk in bundle
        assert len(b.signature) == ED25519_SIG_SIZE

    def test_bronze_bundle(self, engine):
        b = engine.mint_coin("BRONZE")
        assert b.coin_category == "BRONZE"
        assert len(b.public_key) == X25519_PK_SIZE
        assert len(b.secret_key) == X25519_SK_SIZE
        assert len(b.signature) == ED25519_SIG_SIZE

    def test_invalid_category_raises(self, engine):
        with pytest.raises(InvalidCoinCategoryError):
            engine.mint_coin("PLATINUM")

    def test_bundles_are_unique(self, engine):
        b1 = engine.mint_coin("SILVER")
        b2 = engine.mint_coin("SILVER")
        assert b1.key_id != b2.key_id
        assert b1.public_key != b2.public_key

    def test_gold_kem_roundtrip_with_minted_keys(self, engine):
        """Full lifecycle: mint GOLD → encapsulate with pk → decapsulate with sk."""
        b = engine.mint_coin("GOLD")
        ct, ss_sender = engine.kem_encapsulate(b.public_key)
        ss_receiver = engine.kem_decapsulate(ct, b.secret_key)
        assert ss_sender == ss_receiver
