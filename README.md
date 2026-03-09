# ReadMe.md
## Project Overview

AQM Database is the **data persistence layer** for the Amortized Quantum Messaging (AQM) system. It implements a complete post-quantum key lifecycle across three database tiers:

- **Bob's Secure Vault** (local Redis db=0) — stores hardware-encrypted private keys with burn-after-decrypt semantics
- **Alice's Smart Inventory** (local Redis db=1) — caches contacts' public keys with per-contact/per-tier budget caps and FIFO coin selection with tier fallback
- **Server's Coin Inventory** (PostgreSQL) — public key directory with atomic Delete-on-Fetch via `FOR UPDATE SKIP LOCKED`
- **Bridge** — async glue connecting Redis ↔ PostgreSQL (fetch_and_cache, upload_coins, sync_inventory)
- **Crypto Engine** — post-quantum key generation (Kyber-768 + X25519), Dilithium-3/Ed25519 signing, real NaCl AEAD encryption
- **Context Manager** — device-aware coin tier selection based on battery, WiFi, and signal strength
- **Chat** — terminal-to-terminal real-time chat using the full AQM lifecycle, with TLS 1.3 benchmark comparison

## Environment Setup

```bash
conda env create -f AQM_Database/enviroment.yml
conda activate aqm-db
cd AQM_Database && docker compose up -d     # Redis 7 (6379) + PostgreSQL 16 (5433)
```

Python 3.10+. Key deps: `redis-py`, `asyncpg`, `fastapi`, `pynacl`, `pytest`, `fakeredis`.
Optional: `liboqs-python` for real Kyber-768 + Dilithium-3 (falls back to X25519-based mock keygen + random padding without it).

## Running the Demo

```bash
python demo.py                  # preflight checks + 4-phase lifecycle demo
python demo.py --check          # only run preflight checks
python demo.py --tests          # run the full test suite (173 tests)
python demo.py --all            # tests first, then demo
python demo.py --chat           # two-user chat demo (all 3 priority scenarios)
python demo.py --demo-pair      # launch two terminals (default: BESTIE)
python demo.py --demo-pair --priority MATE      # MATE with SILVER ceiling
python demo.py --demo-pair --priority STRANGER  # STRANGER handshake demo
python demo.py --chat-bench     # AQM vs TLS 1.3 benchmark
python -m AQM_Database.prototype  # run demo directly (no preflight)
```

### Chat Demo

```bash
# Launch two terminals automatically (alice + bob)
python demo.py --demo-pair                              # default: BESTIE
python demo.py --demo-pair --priority MATE              # MATE with SILVER ceiling
python demo.py --demo-pair --priority STRANGER          # STRANGER handshake
python -m AQM_Database.chat.cli --demo-pair
python -m AQM_Database.chat.cli --demo-pair --priority MATE

# Interactive two-terminal chat (manual)
# Terminal 1:
python -m AQM_Database.chat.cli --user alice --partner bob --priority BESTIE
# Terminal 2:
python -m AQM_Database.chat.cli --user bob --partner alice --priority BESTIE

# Auto demo — all 3 priority scenarios in one terminal
python demo.py --chat
python -m AQM_Database.chat.cli --auto

# AQM vs TLS 1.3 benchmark
python demo.py --chat-bench
python -m AQM_Database.chat.cli --benchmark
```

## Running Tests

```bash
# All tests (needs Docker for server + chat tests)
pytest AQM_Database/ -v

# By package
pytest AQM_Database/aqm_shared/tests/ -v   # 32 tests — crypto + context (no Docker)
pytest AQM_Database/aqm_db/tests/ -v       # 70 tests — vault, inventory, gc, concurrency (no Docker, uses fakeredis)
pytest AQM_Database/aqm_server/tests/ -v   # 37 tests — upload, fetch, purge, bridge, api (needs Docker)
pytest AQM_Database/chat/tests/ -v         # 34 tests — protocol, session, benchmark (protocol: no Docker; session+benchmark: needs Docker)

# Single test
pytest AQM_Database/aqm_db/tests/test_vault.py::test_store_key_success -v
```

Test total: **173 tests** (32 shared + 70 Redis + 37 server + 34 chat).

## Package Layout

```
AQM_Database/
├── aqm_shared/                    # Shared types, errors, config (used by all)
│   ├── config.py                  # Redis/Vault/Inventory constants, budget caps, enums
│   ├── types.py                   # 11 dataclasses (VaultEntry, InventoryEntry, CoinUpload, …)
│   ├── errors.py                  # Exception hierarchy (AQMDatabaseError base, 10+ subclasses)
│   ├── crypto_engine.py           # CryptoEngine, MintedCoinBundle, mint_coin()
│   ├── context_manager.py         # DeviceContext, ContextManager, SCENARIO_A/B/C
│   └── tests/
│       ├── test_crypto_engine.py  # 15 tests — key sizes, signing (Dilithium/Ed25519), mint_coin
│       └── test_context_manager.py # 17 tests — decision paths, boundaries, scenarios
│
├── aqm_db/                        # Redis client layer
│   ├── connection.py              # create_vault_client(), create_inventory_client(), health_check()
│   ├── vault.py                   # SecureVault — store/burn/fetch/purge private keys
│   ├── inventory.py               # SmartInventory — register contacts, store/select/consume public keys
│   ├── garbage_collector.py       # GarbageCollector — purge inactive contacts
│   ├── stats.py                   # StorageReporter — storage usage, vault report, dashboard
│   └── tests/
│       ├── conftest.py            # fakeredis fixtures (no Docker needed)
│       ├── test_vault.py          # 28 tests
│       ├── test_inventory.py      # 32 tests
│       ├── test_gc.py             # 7 tests (on fakeredis, no Docker needed)
│       └── test_concurrency.py    # 4 tests (threaded, on fakeredis)
│
├── aqm_server/                    # PostgreSQL server layer
│   ├── config.py                  # PG_DSN, pool sizes, maintenance settings
│   ├── db.py                      # create_pool(), get_pool(), close_pool(), health_check()
│   ├── coin_inventory.py          # CoinInventoryServer — upload, fetch, purge, hard_delete
│   ├── api.py                     # FastAPI endpoints (upload, fetch, count, purge, hard-delete)
│   ├── migrations/
│   │   ├── create_coin_inventory.sql
│   │   └── rollback/rollback.sql
│   └── tests/
│       ├── conftest.py            # async fixtures with real PostgreSQL
│       ├── test_upload.py         # 6 tests
│       ├── test_fetch.py          # 9 tests (incl. concurrent fetch)
│       ├── test_purge.py          # 6 tests
│       ├── test_api.py            # 9 FastAPI endpoint tests
│       └── test_bridge.py         # 7 integration tests (Redis ↔ PostgreSQL)
│
├── chat/                          # Terminal-to-terminal real-time chat
│   ├── protocol.py                # ChatMessage, encrypt_message/decrypt_message (NaCl AEAD), JSON serialization
│   ├── transport.py               # Redis pub/sub wrapper (publish/subscribe with threaded listener)
│   ├── session.py                 # ChatSession — full AQM lifecycle per user + run_auto_demo()
│   ├── benchmark.py               # AQM per-tier timing + TLS 1.3 handshake comparison
│   ├── cli.py                     # argparse entry point (--user/--partner/--priority/--auto/--benchmark/--demo-pair)
│   └── tests/
│       ├── conftest.py            # fakeredis + asyncpg fixtures for chat tests
│       ├── test_protocol.py       # 12 tests — serialization, AEAD encrypt/decrypt roundtrip
│       ├── test_session.py        # 14 tests — lifecycle, priorities, exhaustion, burn, coin_status
│       └── test_benchmark.py      # 8 tests — stats, table formatting, TLS handshake, AQM tier, per-message
│
├── bridge.py                      # fetch_and_cache(), upload_coins(), sync_inventory()
├── prototype.py                   # 4-phase lifecycle demo with ANSI terminal output
├── conftest.py                    # Session-scoped event_loop fixture (shared by all async tests)
├── docker-compose.yml             # Redis 7 + PostgreSQL 16
└── enviroment.yml                 # Conda environment spec

demo.py                            # Top-level demo runner with preflight checks
codes/                             # C++ crypto backend reference (liboqs, libsodium)
├── CMakeLists.txt                 # Build config (links liboqs, libsodium, httplib)
├── include/
│   ├── httplib.h                  # HTTP client/server (header-only)
│   └── json.hpp                   # nlohmann JSON (header-only)
└── src/
    ├── crypto/
    │   ├── crypto_engine.cpp      # Kyber-768 + X25519 keygen
    │   └── crypto_engine.h
    ├── common/common.h            # Shared types
    ├── client_module/client_main.cpp
    ├── server_module/server_main.cpp
    └── logic_modules/
        ├── contact_manager.h
        ├── context_manager.h
        └── inventory_manager.h
```

## Architecture

### Module dependency graph

```
aqm_shared/
    config.py ← types.py ← errors.py ← crypto_engine.py
                                       ← context_manager.py

aqm_db/ (Redis)
    connection.py → vault.py → stats.py
                  → inventory.py → garbage_collector.py

aqm_server/ (PostgreSQL)
    db.py → coin_inventory.py → api.py

bridge.py → aqm_db/inventory + aqm_server/coin_inventory

chat/
    protocol.py → transport.py → session.py → cli.py
    benchmark.py → cli.py
    session.py → crypto_engine + context_manager + vault + inventory + server + bridge

prototype.py → crypto_engine + context_manager + vault + inventory + server + bridge
```

### End-to-end data flow

```
┌────────────────────────────────────────────────────────────────────┐
│                       AQM KEY LIFECYCLE                            │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  1. MINT (Bob's device)                                            │
│     CryptoEngine.generate_keypair(tier)                            │
│       ├─ private key → AES-GCM encrypt → SecureVault (Redis db=0) │
│       └─ public key  → sign (Dilithium/Ed25519)                   │
│                        → CoinInventoryServer (PG)                  │
│                                                                    │
│  2. PRE-FETCH (Alice's device)                                     │
│     Bridge.fetch_and_cache(bob_id, tier, count)                    │
│       PG: SELECT ... FOR UPDATE SKIP LOCKED → mark fetched_by     │
│       Redis db=1: SmartInventory.store_key() with budget check     │
│       (WATCH/MULTI/EXEC optimistic locking)                        │
│                                                                    │
│  3. SEND (Alice → Bob)                                             │
│     ContextManager.select_coin(DeviceContext) → tier               │
│     SmartInventory.select_coin(bob, tier) → ZPOPMIN (FIFO)        │
│     encrypt_message(plaintext, pk) → NaCl SecretBox AEAD           │
│     ChatTransport.publish(channel:bob, ChatMessage)                │
│                                                                    │
│  4. RECEIVE (Bob's device)                                         │
│     ChatTransport.subscribe(channel:bob) → ChatMessage             │
│     decrypt_message(ciphertext, pk) → plaintext + verify MAC       │
│     SecureVault.fetch_key(key_id) → private key                    │
│                                                                    │
│  5. BURN (Bob's device)                                            │
│     SecureVault.burn_key(key_id) → status=BURNED + HINCRBY stats   │
│     fetch_key() now returns None — key permanently destroyed       │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│  INVARIANTS                                                        │
│  • Each coin is used exactly once then destroyed                   │
│  • Server coins claimed atomically (FOR UPDATE SKIP LOCKED)        │
│  • Inventory enforces per-contact/per-tier budget caps             │
│  • PQ: GOLD=full, SILVER=partial, BRONZE=classical                 │
└────────────────────────────────────────────────────────────────────┘
```

### Key design patterns

- **Dependency injection**: `SecureVault` and `SmartInventory` receive a `redis.Redis` client via constructor. Tests pass `fakeredis.FakeRedis()`.
- **Binary mode**: Redis clients use `decode_responses=False` — blobs stored as raw bytes. String fields decoded manually in `_deserialize_entry()`.
- **Atomic writes**: All multi-step mutations use `pipeline(transaction=True)` (MULTI/EXEC). Inventory `store_key` uses WATCH/MULTI/EXEC optimistic locking for budget enforcement.
- **Stats tracking**: Vault maintains a `vault:v1:stats` hash with atomic HINCRBY counters (active_gold/silver/bronze, total_burned, total_expired).
- **Sorted set indexes**: Inventory uses sorted sets scored by `fetched_at` for FIFO coin selection via ZPOPMIN.
- **Delete-on-Fetch**: Server uses `FOR UPDATE SKIP LOCKED` to atomically claim coins — fetched coins are marked, not visible to other requesters.
- **Crypto backend fallback**: CryptoEngine tries liboqs+pynacl → pynacl-only → urandom-mock. All backends produce correct-sized keys and signatures.
- **Real AEAD encryption**: `encrypt_message`/`decrypt_message` use NaCl SecretBox (XSalsa20-Poly1305) with key derived from SHA-256(public_key). Falls back to SHA-256 tag if pynacl is unavailable.
- **Constant minting**: All users mint the same set of coins (5G+6S+5B) regardless of priority. Budget caps control how many are cached, and the context manager selects which tier to use at send time. Exception: STRANGER contacts use a handshake flow (mint 1 BRONZE only).
- **Tier ceilings**: Per-priority cap applied after context decision tree: BESTIE=GOLD, MATE=SILVER, STRANGER=BRONZE. Prevents lower-priority contacts from using higher-tier coins even if device context allows it.
- **Absolute imports**: All modules use `from AQM_Database.aqm_shared import config, errors`.
- **pytest-asyncio strict mode**: All async tests need `pytestmark = pytest.mark.asyncio`. Single `event_loop` fixture in `AQM_Database/conftest.py`.
- **Separate pub/sub connections**: Chat transport uses `decode_responses=True` (JSON strings), independent from vault/inventory binary clients.
- **User-specific cleanup**: Chat sessions use targeted DELETE (per user_id) instead of flushdb, so both users coexist on the same Redis/PostgreSQL.

### Redis key namespaces

| Pattern | Type | Purpose |
|---------|------|---------|
| `vault:v1:key:{key_id}` | Hash | Single private key entry |
| `vault:v1:stats` | Hash | Aggregate vault counters |
| `inv:v1:key:{contact_id}:{key_id}` | Hash | Single cached public key |
| `inv:v1:idx:{contact_id}:{GOLD\|SILVER\|BRONZE}` | Sorted Set | Coin selection index |
| `inv:v1:meta:{contact_id}` | Hash | Contact priority/metadata |
| `aqm:chat:{user_id}` | Pub/Sub channel | Real-time message delivery |

### PostgreSQL schema

```sql
coin_inventory (
    record_id      BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL,
    key_id         TEXT NOT NULL,
    coin_category  TEXT NOT NULL,
    public_key_blob BYTEA NOT NULL,
    signature_blob  BYTEA NOT NULL,
    uploaded_at    TIMESTAMPTZ DEFAULT NOW(),
    fetched_by     UUID,
    fetched_at     TIMESTAMPTZ,
    UNIQUE (user_id, key_id)
)
```

### Coin tiers

| Tier | Algorithms | Public Key | Signature | Total |
|------|-----------|-----------|----------|-------|
| GOLD | Kyber-768 + Dilithium-3 | 1,184 B | 2,420 B | ~3.6 KB |
| SILVER | Kyber-768 + Ed25519 | 1,184 B | 64 B | ~1.2 KB |
| BRONZE | X25519 + Ed25519 | 32 B | 64 B | ~96 B |

### Constant mint plan

Every user mints the same set of coins regardless of priority:

| Tier | Count |
|------|-------|
| GOLD | 5 |
| SILVER | 6 |
| BRONZE | 5 |
| **Total** | **16** |

Budget caps control how many of each tier are *cached* (fetched from server to local inventory) per contact priority. The context manager selects which tier to *use* at send time based on device state.

### Budget caps (per contact)

| Priority | Gold | Silver | Bronze |
|----------|------|--------|--------|
| BESTIE | 5 | 4 | 1 |
| MATE | 0 | 6 | 4 |
| STRANGER | 0 | 0 | 5 |

### Context Manager — coin tier selection

```
battery < 5%                    → BRONZE
no WiFi + signal < -100 dBm    → BRONZE
WiFi + battery < 20%           → BRONZE
no WiFi + signal >= -100 dBm   → SILVER
WiFi + 20% <= battery < 50%    → SILVER
WiFi + battery >= 50%          → GOLD
```

### Tier ceilings (per priority)

After the decision tree selects a tier, a per-priority ceiling caps it:

| Priority | Ceiling | Effect |
|----------|---------|--------|
| BESTIE | GOLD | Full range — context tree result used as-is |
| MATE | SILVER | GOLD is capped to SILVER; SILVER/BRONZE pass through |
| STRANGER | BRONZE | Everything capped to BRONZE regardless of context |

### Random context

In the interactive chat demo, device context (battery, WiFi, signal) **fluctuates randomly** between messages — simulating real-world conditions while texting. The context manager decision tree + tier ceiling determine the coin tier per message:

- **BESTIE**: coin tier shifts GOLD → SILVER → BRONZE (and back up) as context changes
- **MATE**: shifts SILVER → BRONZE (and back up); can't exceed SILVER ceiling
- **STRANGER**: always BRONZE regardless of context

## Prototype Demo — 4-phase lifecycle

The prototype (`python demo.py`) demonstrates the full AQM key lifecycle:

1. **MINT** — Generate 16 coins (5G+6S+5B) via CryptoEngine → private keys to Vault, public keys to PostgreSQL server
2. **PRE-FETCH** — Register Bob as BESTIE → fetch public keys from server to local Inventory (budget-capped) → server coins marked as fetched (Delete-on-Fetch)
3. **SEND** — Three device scenarios (A=home WiFi→GOLD, B=outdoor cellular→SILVER, C=underground→BRONZE) → ContextManager selects tier → consume coins from Inventory
4. **DECRYPT+BURN** — Retrieve private key from Vault → burn after use → verify `fetch_key()` returns None

## Chat Demo — real-time two-terminal messaging

The chat demo (`python demo.py --demo-pair` or `python -m AQM_Database.chat.cli --demo-pair`) demonstrates the full AQM lifecycle between two users in real time:

### Chat message lifecycle

1. **ContextManager** inspects device state → selects coin tier
2. **SmartInventory.select_coin()** pops oldest coin (FIFO), with fallback to lower tiers
3. **encrypt_message()** derives symmetric key via SHA-256(pk) → NaCl SecretBox AEAD (XSalsa20-Poly1305)
4. **ChatTransport.publish()** sends JSON envelope via Redis pub/sub
5. Receiver's subscriber callback: deserialize → **decrypt_message()** + verify MAC → **vault.fetch_key()** → **vault.burn_key()** → display with verification + burn status

### Interactive features

- **Real-time display**: incoming messages appear instantly via threaded pub/sub listener
- **Live coin counter**: prompt shows `[G:5 S:4 B:1]` remaining coins
- **Random context**: device state (battery, WiFi, signal) fluctuates randomly per message — coin tier shifts dynamically
- **Tier ceiling**: per-priority cap applied after context decision tree (BESTIE=GOLD, MATE=SILVER, STRANGER=BRONZE)
- **Lifecycle detail**: each message shows key ID, device context, encrypt→publish / decrypt→verify→burn
- **Tier fallback**: displays "wanted GOLD → fell back to SILVER" when tier is unavailable
- **Ceiling cap**: displays "context wanted GOLD → capped to SILVER (ceiling)" when ceiling triggers
- **`--demo-pair`**: auto-detects terminal emulator (tmux/gnome-terminal/konsole/xfce4-terminal/xterm) and spawns both windows
- **`--priority`**: pass `BESTIE`, `MATE`, or `STRANGER` to `--demo-pair` to demo each scenario

### Priority coverage

| Priority | Tier Ceiling | What happens |
|----------|-------------|-------------|
| BESTIE | GOLD | Full range: random context yields GOLD/SILVER/BRONZE dynamically |
| MATE | SILVER | Capped at SILVER: context may want GOLD but ceiling limits to SILVER; shifts SILVER↔BRONZE |
| STRANGER | BRONZE | Always BRONZE: initial handshake (mint 1 BRONZE, share public key), no prefetch |

### STRANGER handshake

STRANGER contacts can't prefetch each other's public keys (first-time texting). Instead of the normal provision+fetch flow:

1. Each side mints **1 BRONZE coin** and uploads the public key to the server
2. Each side polls for the partner's BRONZE coin and fetches it
3. Chat begins with 1 BRONZE coin per direction
4. Tier ceiling ensures all messages use BRONZE regardless of device context

### Benchmark methodology

The benchmark (`python demo.py --chat-bench`) measures three scenarios:

**AQM Full Lifecycle** (per tier, 50 iterations):
```
mint_coin → vault.store_key → upload_coins → fetch_and_cache →
select_coin → encrypt_message → decrypt_message → burn_key
```
Includes one-time minting cost (~2-3ms for crypto keygen). Analogous to a first-contact scenario.
Expected latency ordering: GOLD > SILVER > BRONZE (driven by coin data sizes: 3.6 KB → 1.2 KB → 96 B).

**AQM Per-Message** (pre-minted coins, 50 iterations):
```
select_coin → encrypt_message → decrypt_message → burn_key
```
Coins are pre-minted and cached before timing starts. Reflects steady-state messaging latency.
Per-message consistently beats TLS 1.3 (~0.1-0.3ms vs ~1.7ms) while providing post-quantum resistance (GOLD/SILVER) and perfect forward secrecy (all tiers: single-use keys).

**TLS 1.3** (loopback, 50 iterations):
Ephemeral ECDSA P-256 self-signed cert, measures `ssl.wrap_socket()` handshake time.

Outputs ANSI comparison table with full lifecycle, per-message, byte sizes, and PQ-resistance.

## Docker Setup

```yaml
# AQM_Database/docker-compose.yml
services:
  redis:     redis:7-alpine    → localhost:6379
  postgres:  postgres:16-alpine → localhost:5433
             POSTGRES_DB=aqm, POSTGRES_USER=aqm_user, POSTGRES_PASSWORD=aqm_dev_password
             migrations auto-run via /docker-entrypoint-initdb.d mount
```

## Guides Reference

The `AQM_Database/guides/` directory contains authoritative specs:
- `AQM_Client_DB_Guide.md` — complete API signatures, Redis schemas, transaction patterns
- `AQM_Client_DB_File_Breakdown.md` — exact function specs per file, expected test counts
- `AQM_Database_Implementation_Guide.md` — full system design including server DB
- `AQM_Database_Roadmap.md` — sprint plan and implementation order
- `AQM_Server_PostgreSQL_Guide.md` — server database design
- `AQM_Server_Roadmap.md` — server implementation roadmap
- `AQM_Chat_Demo_Guide.md` — chat demo usage, interactive features, troubleshooting

## Known Issues

- `redis-py 7.x` WATCH/UNWATCH deprecation warnings — cosmetic only, call from Pipeline object to suppress
- `liboqs-python` not in conda environment — Kyber-768 keygen uses real X25519 + random padding (correct sizes, not cryptographically real PQC); Dilithium-3 signatures use Ed25519 core + random padding (correct 2420 B size)
