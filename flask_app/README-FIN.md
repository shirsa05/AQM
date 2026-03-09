# AQM — Amortized Quantum Messaging

A post-quantum secure messaging system that amortizes expensive key encapsulation operations across hundreds of messages using a symmetric ratchet. One ML-KEM-768 operation covers up to 250 messages — providing post-quantum resistance without per-message KEM overhead.

---

## How It Works

Traditional post-quantum messaging performs one KEM operation per message. AQM amortizes this cost:

```
Message 1  → KEM encapsulate (ML-KEM-768) → establish ratchet → encrypt
Message 2  → derive ratchet key            → encrypt  (no KEM)
Message 3  → derive ratchet key            → encrypt  (no KEM)
...
Message 250 → ratchet exhausted → KEM encapsulate again → new ratchet
```

Each coin is a public/private keypair. The sender uses the receiver's public key once to establish a shared secret, then both sides derive message keys via HKDF without further KEM operations until the ratchet limit is reached.

---

## Coin Tiers

Three security tiers with different algorithms and ratchet limits:

| Tier   | KEM Algorithm | Signature    | Ratchet Limit | Public Key Size |
|--------|--------------|--------------|---------------|-----------------|
| GOLD   | ML-KEM-768   | ML-DSA-65    | 250 messages  | ~3.6 KB         |
| SILVER | ML-KEM-768   | Ed25519      | 150 messages  | ~1.2 KB         |
| BRONZE | X25519       | Ed25519      | 75 messages   | ~96 B           |

---

## Context-Based Tier Selection

The device context manager automatically selects the appropriate tier based on real-time device state:

```
battery < 5%                    → BRONZE   (critical battery — conserve)
no WiFi + signal < -100 dBm    → BRONZE   (poor signal — small keys)
WiFi + battery < 20%           → BRONZE   (low battery — conserve)
no WiFi + signal >= -100 dBm   → SILVER   (decent cellular)
WiFi + 20% <= battery < 50%    → SILVER   (moderate conditions)
WiFi + battery >= 50%          → GOLD     (ideal conditions)
```

The selected tier is further capped by the contact's priority level (see below).

---

## Contact Priority System

Contacts are automatically promoted based on messaging frequency:

| Priority | Promotion Condition         | GOLD cap | SILVER cap | BRONZE cap |
|----------|-----------------------------|----------|------------|------------|
| STRANGER | Default                     | 0        | 0          | 5          |
| MATE     | 4 messages within 30 days   | 0        | 6          | 4          |
| BESTIE   | 5 messages within 7 days    | 5        | 4          | 1          |

STRANGER contacts are restricted to BRONZE regardless of device context. As trust increases, higher-tier coins become available.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Web UI                        │
│  (Per-user instance — Alice :5000, Bob :5001, ...)      │
└────────────────┬───────────────────┬────────────────────┘
                 │                   │
    ┌────────────▼──────┐   ┌────────▼──────────┐
    │   SecureVault     │   │  SmartInventory   │
    │  (Redis — local)  │   │  (Redis — local)  │
    │  Own private keys │   │  Partner pub keys │
    │  Burn on decrypt  │   │  Budget-capped    │
    └───────────────────┘   └────────┬──────────┘
                                     │ sync
                            ┌────────▼──────────┐
                            │  CoinInventory    │
                            │  Server (FastAPI) │
                            │  PostgreSQL 16    │
                            │  Delete-on-Fetch  │
                            └───────────────────┘
```

**Vault** — stores your own private keys locally in Redis. Keys are burned (permanently deleted) immediately after a single use for perfect forward secrecy.

**Inventory** — caches your contacts' public keys locally in Redis. Replenished automatically from the coin server in the background.

**Coin Server** — shared PostgreSQL registry of public keys. Uses `FOR UPDATE SKIP LOCKED` for atomic delete-on-fetch so each coin is claimed by exactly one recipient.

**Session Ratchet** — HKDF-based symmetric ratchet persisted in SQLite. Survives restarts and allows hundreds of messages from a single KEM operation.

---

## Project Structure

```
Amortized_Quantum_Messaging/
├── flask_app/
│   ├── app.py                     # Flask UI server (per-user instance)
│   └── templates/index.html       # Web UI (SSE, real-time updates)
├── AQM_Database/
│   ├── aqm_shared/
│   │   ├── config.py              # Constants, budget caps, thresholds
│   │   ├── crypto_engine.py       # ML-KEM-768, ML-DSA-65, X25519, AEAD
│   │   ├── context_manager.py     # Device context → tier selection
│   │   └── types.py               # Shared dataclasses
│   ├── aqm_db/
│   │   ├── vault.py               # Private key storage (Redis)
│   │   ├── inventory.py           # Partner public key cache (Redis)
│   │   └── connection.py          # Redis client factory
│   ├── aqm_contacts/
│   │   ├── contacts_db.py         # Priority + message history (SQLite)
│   │   └── models.py              # Contact dataclass
│   ├── aqm_session/
│   │   ├── ratchet.py             # HKDF ratchet implementation
│   │   └── session_store.py       # Ratchet persistence (SQLite)
│   ├── aqm_server/
│   │   ├── main.py                # FastAPI coin server
│   │   ├── coin_inventory.py      # Server-side coin registry (PostgreSQL)
│   │   └── db.py                  # Async connection pool
│   ├── bridge.py                  # upload_coins / sync_inventory
│   └── environment.yml            # Conda environment spec
├── aqm_bridge.py                  # run_async helper
├── requirements.txt               # Pip dependencies
├── reset_demo.sh                  # Full state reset for fresh demo
└── BUILD.md                       # Detailed build and run guide
```

---

## Quick Start

### 1 — System dependencies

```bash
brew install redis postgresql@16 liboqs
brew services start redis
brew services start postgresql@16
```

### 2 — Python environment

```bash
conda env create -f AQM_Database/environment.yml
conda activate aqm-db
pip install -r requirements.txt
```

### 3 — Database setup

```bash
createdb aqm
cd AQM_Database && alembic upgrade head
```

### 4 — Reset and run

```bash
# Reset all state (fresh demo)
chmod +x reset_demo.sh && ./reset_demo.sh

# Terminal 1 — Coin server
uvicorn AQM_Database.aqm_server.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — Alice
python flask_app/app.py --user alice --port 5000 \
  --contacts bob charlie --contact-ports 5001 5002

# Terminal 3 — Bob
python flask_app/app.py --user bob --port 5001 \
  --contacts alice charlie --contact-ports 5000 5002

# Terminal 4 — Charlie
python flask_app/app.py --user charlie --port 5002 \
  --contacts alice bob --contact-ports 5000 5001
```

### 5 — Open browser

| User    | URL                     |
|---------|-------------------------|
| Alice   | http://localhost:5000   |
| Bob     | http://localhost:5001   |
| Charlie | http://localhost:5002   |

---

## Demo UI Features

- **Real-time SSE** — messages appear instantly without polling
- **Session Tier** — shows the tier currently securing the active ratchet session
- **Next Rekey Tier** — shows what tier the next coin would use based on current device context (updates every 8 seconds independently)
- **Ratchet progress bar** — shows how many messages remain before the next rekey
- **Vault burn counter** — tracks private keys destroyed (perfect forward secrecy)
- **Priority promotion bars** — live progress toward MATE and BESTIE thresholds
- **Per-contact coin inventory** — GOLD/SILVER/BRONZE counts shown on each contact card

---

## Running Tests

```bash
# All tests
pytest AQM_Database/ -v

# By subsystem (no Docker required)
pytest AQM_Database/aqm_shared/tests/ -v    # crypto + context manager
pytest AQM_Database/aqm_db/tests/ -v        # vault + inventory (fakeredis)

# Requires PostgreSQL
pytest AQM_Database/aqm_server/tests/ -v    # coin server + bridge
```

---

## Key Design Decisions

**Why amortize KEM operations?** ML-KEM-768 key generation and encapsulation are computationally expensive compared to classical ECDH. For a messaging app sending hundreds of messages per day, performing a full KEM per message would be impractical on battery-constrained devices. The ratchet amortizes this cost to approximately one KEM per 250 messages for GOLD tier.

**Why burn private keys?** Each private key is used exactly once and then deleted. This provides perfect forward secrecy — even if an attacker later compromises the device, past messages cannot be decrypted because the keys no longer exist.

**Why three tiers?** Different device conditions (battery, network quality) warrant different security/efficiency tradeoffs. A device on WiFi with full battery can afford ML-KEM-768; a device with 3% battery in a tunnel should use X25519 to conserve resources.

**Why priority-based caps?** You wouldn't use your strongest post-quantum keys on a stranger. The cap system ensures GOLD coins (scarce, expensive to generate) are reserved for trusted contacts who you communicate with frequently.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `liboqs-python` | ML-KEM-768 + ML-DSA-65 (post-quantum) |
| `PyNaCl` | X25519 + Ed25519 + AEAD (libsodium) |
| `cryptography` | HKDF key derivation |
| `flask` | Web UI server |
| `fastapi` + `uvicorn` | Coin inventory REST API |
| `asyncpg` | Async PostgreSQL driver |
| `redis-py` | Vault + inventory (Redis) |
| `pydantic` | Request/response validation |

See `requirements.txt` for full version-pinned list.
