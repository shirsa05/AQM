# AQM — Amortized Quantum Messaging
## Build & Run Guide

---

## Prerequisites

### 1 — System dependencies (macOS)

```bash
# Package manager
brew install redis postgresql@16 liboqs

# Start services
brew services start redis
brew services start postgresql@16
```

### 2 — Conda environment (recommended)

```bash
# Create and activate environment from project file
conda env create -f AQM_Database/environment.yml
conda activate aqm-db
```

### 3 — Python dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `liboqs-python` requires `liboqs` to be installed system-wide first.
> If `brew install liboqs` is unavailable, build from source:
> https://github.com/open-quantum-safe/liboqs

---

## Database Setup

### PostgreSQL — Coin Server

```bash
# Create database
createdb aqm

# Run migrations (from project root)
cd AQM_Database
alembic upgrade head
```

### Redis — Vault + Inventory

Redis requires no setup. It starts empty and is populated at runtime.
Verify it is running:

```bash
redis-cli ping   # should return PONG
```

---

## Running the Demo (Single Machine — Alice, Bob, Charlie)

### Step 1 — Reset all state (fresh demo)

```bash
# Flush Redis
redis-cli FLUSHALL

# Delete SQLite databases
rm -f ~/.aqm/alice_contacts.db
rm -f ~/.aqm/bob_contacts.db
rm -f ~/.aqm/charlie_contacts.db

# Delete session ratchet databases (run from project root)
rm -f alice_sessions.db bob_sessions.db charlie_sessions.db
```

### Step 2 — Start the Coin Server (PostgreSQL backend)

```bash
# Terminal 1 — from project root
conda activate aqm-db
cd AQM_Database
uvicorn aqm_server.main:app --host 0.0.0.0 --port 8000 --reload
```

### Step 3 — Start Flask instances (one per terminal)

```bash
# Terminal 2 — Alice
conda activate aqm-db
python flask_app/app.py --user alice --port 5000 \
  --contacts bob charlie --contact-ports 5001 5002

# Terminal 3 — Bob
conda activate aqm-db
python flask_app/app.py --user bob --port 5001 \
  --contacts alice charlie --contact-ports 5000 5002

# Terminal 4 — Charlie
conda activate aqm-db
python flask_app/app.py --user charlie --port 5002 \
  --contacts alice bob --contact-ports 5000 5001
```

### Step 4 — Open in browser

| User    | URL                        |
|---------|----------------------------|
| Alice   | http://localhost:5000      |
| Bob     | http://localhost:5001      |
| Charlie | http://localhost:5002      |

---

## Priority Promotion Thresholds

| From     | To      | Condition                        |
|----------|---------|----------------------------------|
| STRANGER | MATE    | 4 messages within 30 days        |
| MATE     | BESTIE  | 5 messages within 7 days         |

Priority unlocks higher-tier coin caps:

| Priority | GOLD cap | SILVER cap | BRONZE cap |
|----------|----------|------------|------------|
| STRANGER | 0        | 0          | 5          |
| MATE     | 0        | 6          | 4          |
| BESTIE   | 5        | 4          | 1          |

---

## Context-Based Tier Selection

The device context simulator updates every 8 seconds with random values.
Tier is selected based on the following decision tree:

```
battery < 5%                    → BRONZE
no WiFi + signal < -100 dBm    → BRONZE
WiFi + battery < 20%           → BRONZE
no WiFi + signal >= -100 dBm   → SILVER
WiFi + 20% <= battery < 50%    → SILVER
WiFi + battery >= 50%          → GOLD
```

**SESSION TIER** — tier the current ratchet session is using (fixed until rekey)
**NEXT REKEY TIER** — tier that would be used if a rekey happened right now

---

## Ratchet Limits (messages per coin)

| Tier   | Messages before rekey |
|--------|-----------------------|
| GOLD   | 250                   |
| SILVER | 150                   |
| BRONZE | 75                    |

---

## Project Structure

```
Amortized_Quantum_Messaging/
├── flask_app/
│   ├── app.py                  # Flask UI server
│   └── templates/index.html   # Web UI
├── AQM_Database/
│   ├── aqm_db/
│   │   ├── vault.py            # Private key storage (Redis)
│   │   ├── inventory.py        # Partner public key cache (Redis)
│   │   ├── connection.py       # Redis client factory
│   │   └── stats.py            # Storage reporter
│   ├── aqm_contacts/
│   │   ├── contacts_db.py      # Priority + message history (SQLite)
│   │   └── models.py           # Contact dataclass
│   ├── aqm_session/
│   │   ├── ratchet.py          # HKDF ratchet implementation
│   │   └── session_store.py    # Ratchet persistence (SQLite)
│   ├── aqm_server/
│   │   ├── main.py             # FastAPI coin server
│   │   ├── coin_inventory.py   # Server-side coin registry
│   │   └── db.py               # PostgreSQL pool
│   ├── aqm_shared/
│   │   ├── crypto_engine.py    # ML-KEM-768, ML-DSA-65, X25519, AEAD
│   │   ├── context_manager.py  # Device context → tier selection
│   │   ├── config.py           # All constants and thresholds
│   │   └── types.py            # Shared dataclasses
│   ├── bridge.py               # upload_coins / sync_inventory
│   └── environment.yml         # Conda environment spec
├── aqm_bridge.py               # run_async helper
├── requirements.txt            # Pip dependencies
├── reset_demo.sh               # Full state reset script
└── BUILD.md                    # This file
```

---

## Troubleshooting

**`liboqs` version mismatch warning**
```
UserWarning: liboqs version 0.15.0 differs from liboqs-python version 0.14.1
```
Cosmetic only — does not affect functionality. To fix, align versions:
```bash
pip install liboqs-python==0.15.0
```

**Flask starts twice (debug mode)**
Ensure `debug=False` in `app.py`:
```python
app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
```

**Inventory shows 0 coins after start**
Partner Flask instance must be started first so their coins are on the server.
Background sync retries every 10 seconds automatically.

**Priority shows BESTIE on fresh start**
SQLite was not fully deleted. Run the reset script:
```bash
./reset_demo.sh
```
