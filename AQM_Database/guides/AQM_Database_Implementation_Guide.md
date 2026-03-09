# AQM Database Implementation Guide

## Your Scope Within the AQM Project

You are responsible for the **data persistence layer** of the Amortized Quantum Messaging system. Three databases fall under your ownership:

| Database | Location | Technology | Purpose |
|----------|----------|------------|---------|
| **Bob's Secure Vault** | Recipient device (local) | Redis (primary) / SQLite (alternative) | Stores private keys encrypted by hardware key |
| **Alice's Smart Inventory** | Sender device (local) | Redis (primary) / SQLite (alternative) | Caches public keys + signatures for contacts |
| **Server's Coin Inventory** | Central server | PostgreSQL (primary) / CockroachDB (alternative) | Public key directory — the "Blind Courier" backbone |

Your code must expose clean interfaces that the Coin Minting module (key generation), the Crypto Wrapper module (Kyber/Dilithium/X25519 operations), and the Context Manager (battery/signal logic) will call. You do **not** implement those modules — you store and retrieve what they produce.

---

## 1. Bob's Secure Vault (Local — Recipient Device)

### 1.1 What It Stores

Every key Bob's device mints gets a private half stored here. When Alice's encrypted parcel arrives referencing a specific `Key ID`, Bob looks up that row, decrypts the private key blob using the device hardware key, and hands it to the Crypto Wrapper for decapsulation.

### 1.2 Schema

```
Key: vault:{key_id}
Type: Redis Hash

Fields:
  coin_category   → "GOLD" | "SILVER" | "BRONZE"
  encrypted_blob  → <binary> (private key encrypted by hardware AES key)
  encryption_iv   → <12 bytes> (AES-GCM nonce used during hardware encryption)
  auth_tag        → <16 bytes> (AES-GCM authentication tag)
  status          → "ACTIVE" | "BURNED"
  created_at      → Unix timestamp (ms)
```

**Why Redis?** Private key lookups must be sub-millisecond. When a parcel arrives, the app needs to find Key #10405, decrypt, and respond. Redis hash lookups are O(1). The dataset is tiny (tens of keys, each ~2 KB) — it fits entirely in memory.

### 1.3 Operations You Must Implement

| Operation | Redis Command Pattern | Called By |
|-----------|-----------------------|-----------|
| **Store new key** | `HSET vault:{key_id} coin_category ... status ACTIVE` | Coin Minting module (Phase 1) |
| **Lookup by Key ID** | `HGETALL vault:{key_id}` | Message Receiver (Phase 3) |
| **Burn after use** | `HSET vault:{key_id} status BURNED` then `DEL vault:{key_id}` after grace period | Message Receiver (Phase 3) |
| **Count active keys by tier** | `SCAN` with pattern `vault:*` + filter by `coin_category` | Minting scheduler (decides when to mint more) |
| **Purge expired** | Iterate and delete where `created_at` < threshold | Background cron / TTL |

### 1.4 Critical Implementation Details

**Replay attack prevention:** Once a key decrypts a message, it must immediately flip to `BURNED`. The Crypto Wrapper will call your `burn(key_id)` function. You should implement this as a two-step process: first set `status = BURNED` (so a concurrent lookup fails), then schedule deletion after a short grace period (e.g., 5 seconds) to handle retransmissions.

**Hardware encryption is NOT your job.** The Crypto Wrapper handles encrypting the raw private key with the device's hardware-bound AES key. You receive an opaque `encrypted_blob`, `iv`, and `auth_tag`. Store them as-is. Never attempt to interpret the blob.

**Persistence:** Redis alone is volatile. For a production device, enable Redis AOF (Append-Only File) persistence or use Redis with RDB snapshots. Alternatively, use the SQLite fallback (see Section 4).

**TTL strategy:** Set a Redis TTL of 30 days on each key hash. If a key hasn't been consumed in 30 days, it's stale — the server copy should also be purged.

```bash
EXPIRE vault:{key_id} 2592000   # 30 days in seconds
```

### 1.5 Interface Contract (Pseudocode)

```python
class SecureVault:
    def store_key(key_id: str, coin_category: str, encrypted_blob: bytes,
                  iv: bytes, auth_tag: bytes) -> bool

    def fetch_key(key_id: str) -> Optional[VaultEntry]
        # Returns None if key doesn't exist or is BURNED

    def burn_key(key_id: str) -> bool
        # Atomic: sets BURNED + schedules deletion

    def count_active(coin_category: str) -> int

    def purge_expired(max_age_days: int = 30) -> int
        # Returns number of keys purged
```

---

## 2. Alice's Smart Inventory (Local — Sender Device)

### 2.1 What It Stores

Alice pre-fetches Bob's (and other contacts') public keys from the server so that when she wants to message, there's zero network round-trip. The Smart Inventory is organized by contact priority (Bestie / Mate / Stranger) and coin tier (Gold / Silver / Bronze).

### 2.2 Schema

```
Key: inventory:{contact_id}:{key_id}
Type: Redis Hash

Fields:
  coin_category    → "GOLD" | "SILVER" | "BRONZE"
  public_key       → <binary> (Kyber or X25519 public key)
  signature        → <binary> (Dilithium or Ed25519 signature)
  contact_priority → "BESTIE" | "MATE" | "STRANGER"
  fetched_at       → Unix timestamp (ms)
  last_used_at     → Unix timestamp (ms)  # updated on each use for LRU

Secondary Index (Sorted Set for fast tier selection):
  Key: idx:inventory:{contact_id}:{coin_category}
  Members: key_id values, scored by fetched_at
```

### 2.3 Storage Budget Enforcement

The paper specifies strict per-contact budgets:

| Priority | Gold | Silver | Bronze | Total Est. Storage |
|----------|------|--------|--------|--------------------|
| **Bestie** (top 5, daily msgs) | 5 | 4 | 1 | ~25.8 KB/person |
| **Mate** (weekly) | 0 | 6 | 4 | ~9.2 KB/person |
| **Stranger** (new/monthly) | 0 | 0 | 0 | 0 KB (fetch-on-demand) |

Your database layer must enforce these caps. When the Pre-fetching module (Phase 2) tries to store a 6th Gold Coin for a Bestie, your code should reject it or evict the oldest.

### 2.4 Operations You Must Implement

| Operation | Description | Called By |
|-----------|-------------|-----------|
| **Store pre-fetched key** | Validate budget cap → store hash + update sorted set index | Pre-fetch scheduler (Phase 2) |
| **Select best available coin** | Given `contact_id` and desired `coin_category`, pop the oldest key from the sorted set | Message Sender (Phase 3) — the Context Manager tells it which tier to use |
| **Consume (delete after send)** | Remove hash + remove from sorted set | Message Sender (Phase 3) |
| **Get inventory summary** | Count keys per contact per tier | UI display / minting scheduler |
| **Garbage collect** | Delete keys for contacts with `last_used_at` > 30 days | Background cron |
| **Upgrade contact priority** | Reclassify a contact (e.g., Stranger → Mate) and adjust caps | Contact frequency analyzer |

### 2.5 Coin Selection Algorithm

When Alice sends a message, the Context Manager determines the ideal tier (Gold / Silver / Bronze) based on battery and signal. Your job is to return the best available key:

```
function selectCoin(contact_id, desired_tier):
    # Try desired tier first
    key_id = ZPOPMIN(idx:inventory:{contact_id}:{desired_tier})
    if key_id exists:
        entry = HGETALL(inventory:{contact_id}:{key_id})
        DELETE(inventory:{contact_id}:{key_id})
        return entry

    # Fallback: try one tier down
    fallback_order = {GOLD: [SILVER, BRONZE], SILVER: [BRONZE], BRONZE: []}
    for fallback_tier in fallback_order[desired_tier]:
        key_id = ZPOPMIN(idx:inventory:{contact_id}:{fallback_tier})
        if key_id exists:
            entry = HGETALL(inventory:{contact_id}:{key_id})
            DELETE(inventory:{contact_id}:{key_id})
            return entry

    # No cached key — trigger on-demand fetch from server
    return None  # signals the network layer to fetch
```

### 2.6 Interface Contract (Pseudocode)

```python
class SmartInventory:
    def store_key(contact_id: str, key_id: str, coin_category: str,
                  public_key: bytes, signature: bytes,
                  contact_priority: str) -> bool
        # Enforces per-contact, per-tier budget caps
        # Returns False if budget exceeded

    def select_coin(contact_id: str, desired_tier: str) -> Optional[InventoryEntry]
        # Atomic pop: returns key and removes from inventory

    def get_summary(contact_id: str = None) -> dict
        # {contact_id: {GOLD: n, SILVER: n, BRONZE: n}}

    def set_priority(contact_id: str, priority: str) -> bool

    def garbage_collect(inactive_days: int = 30) -> int

    def get_total_storage_bytes() -> int
        # For the UI to show "X KB used for key cache"
```

---

## 3. Server's Coin Inventory (Central — PostgreSQL)

### 3.1 What It Stores

The server is the "Blind Courier." It holds public keys that devices have minted and uploaded, and serves them to senders who request keys for a specific recipient. The server never sees private keys. After a key is downloaded by a sender, it should be deleted ("Delete-on-Fetch" from the roadmap).

### 3.2 Schema

```sql
CREATE TABLE coin_inventory (
    record_id       BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,            -- Who minted this key (e.g., Bob)
    key_id          VARCHAR(32) NOT NULL,      -- Client-side serial (e.g., #10405)
    coin_category   VARCHAR(6) NOT NULL        -- 'GOLD', 'SILVER', 'BRONZE'
                    CHECK (coin_category IN ('GOLD', 'SILVER', 'BRONZE')),
    public_key_blob BYTEA NOT NULL,            -- Kyber or X25519 public key
    signature_blob  BYTEA NOT NULL,            -- Dilithium or Ed25519 signature
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_by      UUID DEFAULT NULL,         -- NULL until downloaded; set on fetch
    fetched_at      TIMESTAMPTZ DEFAULT NULL,

    CONSTRAINT uq_user_key UNIQUE (user_id, key_id)
);

-- Primary query: "Give me Silver keys for Bob"
CREATE INDEX idx_coin_lookup
    ON coin_inventory (user_id, coin_category)
    WHERE fetched_by IS NULL;

-- Hygiene: find expired unfetched keys
CREATE INDEX idx_coin_expiry
    ON coin_inventory (uploaded_at)
    WHERE fetched_by IS NULL;
```

### 3.3 Why PostgreSQL?

The server handles concurrent requests from many devices — Alice and thousands of others requesting Bob's keys simultaneously. PostgreSQL provides ACID transactions (critical for Delete-on-Fetch atomicity), partial indexes (the `WHERE fetched_by IS NULL` clause means queries only scan unfetched keys), `BYTEA` for binary blobs without encoding overhead, and row-level locking to prevent two senders from grabbing the same key.

### 3.4 Operations You Must Implement

| Operation | SQL Pattern | Called By |
|-----------|-------------|-----------|
| **Upload keys (batch)** | `INSERT INTO coin_inventory (...) VALUES ...` (batch of coins from one device) | Minting upload endpoint |
| **Fetch keys for contact** | `SELECT ... WHERE user_id = $1 AND coin_category = $2 AND fetched_by IS NULL ORDER BY uploaded_at LIMIT $3 FOR UPDATE SKIP LOCKED` | Pre-fetch endpoint / on-demand fetch |
| **Delete-on-Fetch** | `UPDATE ... SET fetched_by = $1, fetched_at = NOW() WHERE record_id = $2` then hard-delete after grace period | Same as above (within transaction) |
| **Purge stale keys** | `DELETE FROM coin_inventory WHERE uploaded_at < NOW() - INTERVAL '30 days' AND fetched_by IS NULL` | Scheduled cron job |
| **Inventory count** | `SELECT coin_category, COUNT(*) FROM coin_inventory WHERE user_id = $1 AND fetched_by IS NULL GROUP BY coin_category` | Device sync / minting scheduler |

### 3.5 Delete-on-Fetch: The Critical Transaction

This is the most important operation on the server. Two senders must never receive the same key (that would allow one to impersonate the other). Use `FOR UPDATE SKIP LOCKED`:

```sql
BEGIN;

-- Atomically claim up to 3 Silver keys for Bob
WITH claimed AS (
    SELECT record_id, key_id, public_key_blob, signature_blob
    FROM coin_inventory
    WHERE user_id = :bob_id
      AND coin_category = 'SILVER'
      AND fetched_by IS NULL
    ORDER BY uploaded_at ASC
    LIMIT 3
    FOR UPDATE SKIP LOCKED
)
UPDATE coin_inventory ci
SET fetched_by = :alice_id, fetched_at = NOW()
FROM claimed
WHERE ci.record_id = claimed.record_id
RETURNING claimed.key_id, claimed.public_key_blob, claimed.signature_blob;

COMMIT;
```

The `SKIP LOCKED` ensures that if another sender is concurrently claiming keys, the query doesn't block — it simply skips already-locked rows and picks the next available ones.

### 3.6 Hard Delete Schedule

Soft-delete (setting `fetched_by`) happens immediately. Hard-delete should run periodically to reclaim storage:

```sql
-- Run every hour via pg_cron or application scheduler
DELETE FROM coin_inventory
WHERE fetched_at IS NOT NULL
  AND fetched_at < NOW() - INTERVAL '1 hour';
```

### 3.7 Interface Contract (Pseudocode)

```python
class CoinInventoryServer:
    def upload_coins(user_id: UUID, coins: list[CoinUpload]) -> int
        # Batch insert. Returns count of successfully stored coins.
        # Rejects duplicates on (user_id, key_id).

    def fetch_coins(target_user_id: UUID, requester_id: UUID,
                    coin_category: str, count: int) -> list[CoinRecord]
        # Atomic claim + soft-delete. Returns claimed coins.

    def get_inventory_count(user_id: UUID) -> dict[str, int]
        # {GOLD: n, SILVER: n, BRONZE: n} of unfetched keys

    def purge_stale(max_age_days: int = 30) -> int
        # Deletes unfetched keys older than threshold

    def hard_delete_fetched(grace_hours: int = 1) -> int
        # Removes soft-deleted rows
```

---

## 4. Alternative Technologies

### 4.1 Local Database Alternatives

| Technology | Pros | Cons | Best For |
|------------|------|------|----------|
| **Redis** (recommended) | Sub-ms lookups, built-in TTL, atomic ops, sorted sets for indexing | Volatile without AOF/RDB; heavier memory footprint | Devices with ≥512 MB RAM (most modern phones) |
| **SQLite** | Zero-config, file-based, battle-tested on mobile (Android/iOS native), tiny footprint | No built-in TTL, slower for high-frequency random reads | Ultra-constrained IoT devices, or if Redis feels too heavy |
| **LevelDB / RocksDB** | Fast key-value with on-disk persistence, good write throughput | No query language, manual index management | Embedded systems needing write-heavy workloads |
| **Realm** | Mobile-native ORM, reactive queries, encryption built-in | Vendor lock-in, heavier SDK | If your team is building native mobile apps |

**SQLite Fallback Schema (Secure Vault):**

```sql
CREATE TABLE secure_vault (
    key_id          TEXT PRIMARY KEY,
    coin_category   TEXT NOT NULL CHECK (coin_category IN ('GOLD', 'SILVER', 'BRONZE')),
    encrypted_blob  BLOB NOT NULL,
    encryption_iv   BLOB NOT NULL,
    auth_tag        BLOB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'BURNED')),
    created_at      INTEGER NOT NULL  -- Unix epoch ms
);

CREATE INDEX idx_vault_status ON secure_vault (status) WHERE status = 'ACTIVE';
```

**SQLite Fallback Schema (Smart Inventory):**

```sql
CREATE TABLE smart_inventory (
    contact_id       TEXT NOT NULL,
    key_id           TEXT NOT NULL,
    coin_category    TEXT NOT NULL CHECK (coin_category IN ('GOLD', 'SILVER', 'BRONZE')),
    public_key       BLOB NOT NULL,
    signature        BLOB NOT NULL,
    contact_priority TEXT NOT NULL CHECK (contact_priority IN ('BESTIE', 'MATE', 'STRANGER')),
    fetched_at       INTEGER NOT NULL,
    last_used_at     INTEGER NOT NULL,
    PRIMARY KEY (contact_id, key_id)
);

CREATE INDEX idx_inv_selection
    ON smart_inventory (contact_id, coin_category, fetched_at ASC);

CREATE INDEX idx_inv_gc
    ON smart_inventory (last_used_at ASC);
```

### 4.2 Server Database Alternatives

| Technology | Pros | Cons | Best For |
|------------|------|------|----------|
| **PostgreSQL** (recommended) | ACID, partial indexes, `FOR UPDATE SKIP LOCKED`, mature ecosystem | Single-node write bottleneck without sharding | Up to ~10M users, single-region |
| **CockroachDB** | PostgreSQL-compatible, distributed, auto-sharding, survives node failures | Higher write latency (~10ms vs ~1ms), operational complexity | Multi-region, >10M users |
| **ScyllaDB** | Ultra-low latency at massive scale, compatible with Cassandra drivers | Eventual consistency (dangerous for Delete-on-Fetch atomicity without LWT) | Read-heavy workloads where you can tolerate careful consistency tuning |
| **TiDB** | MySQL-compatible, distributed | Less mature ecosystem for binary blob handling | Teams already on MySQL |

**CockroachDB schema is nearly identical to PostgreSQL** — the main adjustment is using `UUID` as primary key instead of `BIGSERIAL` for distributed ID generation:

```sql
CREATE TABLE coin_inventory (
    record_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- ... rest identical to PostgreSQL schema
);
```

---

## 5. Cross-Cutting Concerns

### 5.1 Data Sizes (Know Your Blobs)

Understanding blob sizes is critical for storage budgeting and choosing between Redis and SQLite:

| Coin Type | Public Key Size | Signature Size | Total per Coin |
|-----------|----------------|----------------|----------------|
| GOLD (Kyber-768 + Dilithium) | 1,184 bytes (Kyber-768 pk) | 2,420 bytes (Dilithium sig) | ~3.6 KB |
| SILVER (Kyber-768 + Ed25519) | 1,184 bytes (Kyber-768 pk) | 64 bytes (Ed25519 sig) | ~1.2 KB |
| BRONZE (X25519 + Ed25519) | 32 bytes (X25519 pk) | 64 bytes (Ed25519 sig) | ~0.1 KB |

Private keys (Secure Vault) after hardware encryption will be slightly larger than the raw private key due to AES-GCM overhead (+12 bytes IV + 16 bytes tag).

### 5.2 Concurrency

**Local databases:** Only one app process accesses the local DB at a time (single user, single device). Concurrency is minimal — your main concern is ensuring the message-receive handler and the background minting scheduler don't race on the same key. Use Redis transactions (`MULTI/EXEC`) or SQLite's WAL mode.

**Server database:** High concurrency is the norm. The `FOR UPDATE SKIP LOCKED` pattern in PostgreSQL is your primary defense against double-fetch. Always wrap fetch operations in explicit transactions.

### 5.3 Encryption at Rest

The Secure Vault's `encrypted_blob` is already encrypted by the hardware key before it reaches you. But the Smart Inventory stores **plaintext public keys** — these aren't secret (they're public), but the metadata (who Alice talks to, how often) is sensitive. Consider encrypting the entire SQLite file with SQLCipher or using Redis encryption at rest if the threat model requires it.

### 5.4 Migration Strategy

As AQM evolves (new coin types, new fields), you'll need schema migrations:

- **Redis:** No formal migrations. Use versioned key prefixes (`v2:vault:{key_id}`) and write migration scripts that re-key data.
- **SQLite:** Use a migration tool like `golang-migrate` or a simple version table.
- **PostgreSQL:** Use `Flyway`, `Alembic` (Python), or `golang-migrate`. Always test migrations against a copy of production data.

---

## 6. Testing Strategy

### 6.1 Unit Tests

- **Budget enforcement:** Store 5 Gold keys for a Bestie, attempt a 6th — assert rejection.
- **Burn semantics:** Burn a key, attempt fetch — assert `None`.
- **Coin selection fallback:** Request Gold when only Silver exists — assert Silver returned.
- **TTL expiry:** Create a key with a past timestamp, run `purge_expired` — assert deletion.

### 6.2 Integration Tests

- **Delete-on-Fetch atomicity:** Spin up two concurrent fetch requests for the same user — assert no key is returned to both.
- **Upload → Fetch → Burn lifecycle:** Mint on device B, upload to server, fetch from device A, send message, burn on device B.
- **Garbage collection:** Populate inventory with stale keys, run GC, verify freed storage.

### 6.3 Load Tests (Server)

- Simulate 10,000 concurrent fetch requests against a single user's key pool.
- Measure p99 latency of `fetch_coins` under load.
- Verify zero duplicate key assignments.

---

## 7. Dependency Boundaries

Here's exactly what you receive from and provide to other modules:

```
┌─────────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   Coin Minting      │     │   YOUR DATABASE   │     │   Crypto Wrapper    │
│   Module            │────▶│   LAYER           │────▶│   Module            │
│                     │     │                   │     │                     │
│ Produces:           │     │ Stores/retrieves: │     │ Consumes:           │
│ - encrypted_blob    │     │ - Vault entries   │     │ - encrypted_blob    │
│ - iv, auth_tag      │     │ - Inventory keys  │     │ - public_key        │
│ - public_key        │     │ - Server coins    │     │ - signature         │
│ - signature         │     │                   │     │                     │
│ - coin_category     │     │ Enforces:         │     │ Returns:            │
│ - key_id            │     │ - Budget caps     │     │ - shared secret     │
│                     │     │ - Replay prevent  │     │ - decrypted message │
└─────────────────────┘     │ - TTL/expiry      │     └─────────────────────┘
                            │ - Delete-on-Fetch │
┌─────────────────────┐     │                   │     ┌─────────────────────┐
│   Context Manager   │     │                   │     │   Network Layer     │
│                     │────▶│                   │────▶│                     │
│ Tells you:          │     │                   │     │ Calls:              │
│ - desired coin tier │     │                   │     │ - upload_coins()    │
│ - contact_priority  │     └──────────────────┘     │ - fetch_coins()     │
└─────────────────────┘                               └─────────────────────┘
```

**You own the data contracts.** Define the `VaultEntry`, `InventoryEntry`, and `CoinRecord` data structures and share them with your teammates. They code to your interface.
