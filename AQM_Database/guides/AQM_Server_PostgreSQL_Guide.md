# AQM Server Database — PostgreSQL Implementation Guide

## What You're Building

The Server's Coin Inventory. The "Blind Courier" backbone. It holds public keys that devices mint and upload, serves them to senders who request keys for a recipient, and deletes them after download. The server **never** sees private keys or message content.

---

## Part 1: PostgreSQL Concepts You'll Actually Use

Before diving into AQM-specific code, here's what matters for this project. Skip anything you already know.

### 1.1 Connection Flow

```
Your Python App
      │
      ▼
  asyncpg / psycopg3  (driver — translates Python calls to PostgreSQL wire protocol)
      │
      ▼
  Connection Pool     (reuses connections instead of opening/closing per query)
      │
      ▼
  PostgreSQL Server   (running in Docker or cloud)
      │
      ▼
  aqm database → coin_inventory table
```

**Why a connection pool?** Opening a PostgreSQL connection takes ~50ms (TCP + auth + TLS). With 1000 concurrent users, that's 1000 connections. A pool keeps ~20 connections open and recycles them. Use `asyncpg.create_pool()` or PgBouncer.

### 1.2 Transactions — Why They Matter Here

A transaction groups commands into an all-or-nothing unit:

```sql
BEGIN;
  SELECT ... FOR UPDATE;   -- lock rows
  UPDATE ... SET fetched_by = alice;  -- claim them
COMMIT;  -- both succeed, or neither does
```

If your app crashes between SELECT and UPDATE, PostgreSQL automatically rolls back. Without transactions, Alice could see keys that another sender already claimed.

### 1.3 Row Locking — The Core of Delete-on-Fetch

```sql
-- Regular SELECT: reads data, no lock. Other queries can read AND modify same rows.
SELECT * FROM coin_inventory WHERE user_id = 'bob';

-- FOR UPDATE: locks selected rows. Other transactions WAIT until you COMMIT.
SELECT * FROM coin_inventory WHERE user_id = 'bob' FOR UPDATE;

-- FOR UPDATE SKIP LOCKED: locks rows, but if a row is already locked by
-- another transaction, SKIP it instead of waiting. This is the key to
-- high-concurrency Delete-on-Fetch.
SELECT * FROM coin_inventory WHERE user_id = 'bob' FOR UPDATE SKIP LOCKED;
```

**Why SKIP LOCKED?** Two senders request Bob's keys at the same time:
- Sender A locks rows 1,2,3
- Sender B would normally WAIT. With SKIP LOCKED, B gets rows 4,5,6 instead.
- No blocking, no duplicates.

### 1.4 Partial Indexes — Query Only What Matters

```sql
-- Normal index: covers ALL rows
CREATE INDEX idx_all ON coin_inventory (user_id, coin_category);

-- Partial index: covers only unfetched rows
CREATE INDEX idx_unfetched ON coin_inventory (user_id, coin_category)
    WHERE fetched_by IS NULL;
```

99% of your queries filter on `WHERE fetched_by IS NULL`. A partial index only includes those rows, so it's smaller and faster. Once a row is fetched (soft-deleted), it drops out of the index automatically.

### 1.5 BYTEA — Storing Binary Blobs

PostgreSQL's `BYTEA` type stores raw bytes. Your public keys and signatures are binary — don't base64-encode them, just store as BYTEA.

```python
# asyncpg handles bytes → BYTEA automatically
await conn.execute(
    "INSERT INTO coin_inventory (public_key_blob) VALUES ($1)",
    b"\x04\xab\xcd..."  # raw bytes, no encoding needed
)

# reading back: you get bytes directly
row = await conn.fetchrow("SELECT public_key_blob FROM coin_inventory WHERE ...")
pk = row["public_key_blob"]  # bytes object
```

### 1.6 BIGSERIAL vs UUID for Primary Keys

```sql
-- BIGSERIAL: auto-incrementing integer. Simple, fast, compact.
record_id BIGSERIAL PRIMARY KEY  -- 1, 2, 3, 4, ...

-- UUID: globally unique. No coordination needed between servers.
record_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
```

**For AQM:** Use `BIGSERIAL` now (single server). Switch to `UUID` if you move to CockroachDB later.

---

## Part 2: Schema

### 2.1 Main Table

```sql
CREATE TABLE coin_inventory (
    -- ── Identity ──
    record_id       BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,              -- who minted this (Bob)
    key_id          VARCHAR(36) NOT NULL,        -- client-side ID (UUID from device)

    -- ── Coin Data ──
    coin_category   VARCHAR(6) NOT NULL
                    CHECK (coin_category IN ('GOLD', 'SILVER', 'BRONZE')),
    public_key_blob BYTEA NOT NULL,             -- Kyber pk (1184B) or X25519 pk (32B)
    signature_blob  BYTEA NOT NULL,             -- Dilithium sig (2420B) or Ed25519 sig (64B)

    -- ── Lifecycle ──
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_by      UUID DEFAULT NULL,          -- NULL = available; set = claimed
    fetched_at      TIMESTAMPTZ DEFAULT NULL,

    -- ── Constraints ──
    CONSTRAINT uq_user_key UNIQUE (user_id, key_id)
);
```

**Why each column exists:**

| Column | Purpose |
|--------|---------|
| `record_id` | Internal row ID. Never exposed to clients. Used for DELETE. |
| `user_id` | Routing: "give me keys for Bob" → `WHERE user_id = bob` |
| `key_id` | Client sync: Alice puts this in the parcel so Bob knows which private key to use |
| `coin_category` | Filtering: Alice on 2G asks for SILVER only |
| `public_key_blob` | The actual payload Alice needs to encrypt |
| `signature_blob` | Proof the key came from Bob, not a MITM |
| `uploaded_at` | Hygiene: delete keys sitting unfetched for >30 days |
| `fetched_by` | Soft-delete marker: NULL = available, UUID = claimed by someone |
| `fetched_at` | When it was claimed. Used to schedule hard-delete. |
| `uq_user_key` | Prevents duplicate uploads. If Bob's device retries, INSERT doesn't create dupes. |

### 2.2 Indexes

```sql
-- Primary query: "Give me N unfetched Silver keys for Bob"
-- Partial: only indexes rows where fetched_by IS NULL
-- As keys get fetched, they drop out of this index automatically
CREATE INDEX idx_coin_lookup
    ON coin_inventory (user_id, coin_category, uploaded_at ASC)
    WHERE fetched_by IS NULL;

-- Hygiene: find old unfetched keys to purge
CREATE INDEX idx_coin_expiry
    ON coin_inventory (uploaded_at)
    WHERE fetched_by IS NULL;

-- Hard-delete: find fetched keys past grace period
CREATE INDEX idx_coin_hard_delete
    ON coin_inventory (fetched_at)
    WHERE fetched_by IS NOT NULL;
```

### 2.3 Migration File

```
migrations/
├── 001_create_coin_inventory.sql    ← CREATE TABLE + indexes
├── 002_add_some_column.sql          ← future changes
└── rollback/
    └── 001_rollback.sql             ← DROP TABLE coin_inventory
```

Use Alembic (Python) or raw SQL files with a version table:

```sql
CREATE TABLE schema_version (
    version     INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);

-- After running migration 001:
INSERT INTO schema_version (version, description)
VALUES (1, 'Create coin_inventory table');
```

---

## Part 3: Operations

### 3.1 Upload Coins (Batch Insert)

**When:** Bob's device mints coins and uploads public halves.
**Called by:** Network layer's `POST /upload` endpoint.

```
Input:  user_id (UUID), list of {key_id, coin_category, public_key_blob, signature_blob}
Output: count of successfully inserted coins

SQL Pattern:
    INSERT INTO coin_inventory (user_id, key_id, coin_category, public_key_blob, signature_blob)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id, key_id) DO NOTHING  -- idempotent: retry-safe

Batch strategy:
    Use executemany() or a single INSERT with multiple VALUES rows.
    asyncpg supports executemany() natively — one round-trip for N inserts.
```

**Edge cases:**
- Duplicate key_id from device retry → `ON CONFLICT DO NOTHING` silently skips
- Invalid coin_category → CHECK constraint rejects, raise validation error
- Blob too large → add application-level size checks before INSERT

### 3.2 Fetch Coins (Delete-on-Fetch) ⚠️ CRITICAL

**When:** Alice requests Bob's public keys (pre-fetch or on-demand).
**Called by:** Network layer's `GET /fetch` endpoint.

This is the most important query in the entire server. Two senders must NEVER receive the same key.

```
Input:  target_user_id, requester_id, coin_category, count
Output: list of {key_id, public_key_blob, signature_blob}

SQL Pattern (single atomic query using CTE):

    WITH claimed AS (
        SELECT record_id, key_id, public_key_blob, signature_blob
        FROM coin_inventory
        WHERE user_id = $1              -- target (Bob)
          AND coin_category = $2        -- requested tier
          AND fetched_by IS NULL        -- only unclaimed keys
        ORDER BY uploaded_at ASC        -- oldest first (FIFO)
        LIMIT $3                        -- how many Alice wants
        FOR UPDATE SKIP LOCKED          -- lock claimed rows, skip contested ones
    )
    UPDATE coin_inventory ci
    SET fetched_by = $4,                -- who claimed (Alice)
        fetched_at = NOW()
    FROM claimed
    WHERE ci.record_id = claimed.record_id
    RETURNING claimed.key_id,
              claimed.public_key_blob,
              claimed.signature_blob;
```

**Why CTE (WITH ... AS)?** Combines SELECT + UPDATE in one atomic statement. No gap between "find keys" and "mark as claimed" where another sender could sneak in.

**Why ORDER BY uploaded_at ASC?** FIFO — oldest keys consumed first. Prevents keys from sitting forever while newer ones get picked.

**Why SKIP LOCKED?** If Sender A is mid-transaction claiming rows 1-3, Sender B doesn't wait — it skips those and grabs rows 4-6. Zero blocking under concurrency.

**What if fewer keys are available than requested?** The query returns however many it finds. If Alice asks for 4 Silver but only 2 exist, she gets 2. The caller must handle partial results.

### 3.3 Get Inventory Count

**When:** Device sync — Bob's phone checks how many of his keys are still on the server.
**Called by:** Minting scheduler (via network), to decide whether to mint more.

```
Input:  user_id
Output: {GOLD: n, SILVER: n, BRONZE: n}

SQL:
    SELECT coin_category, COUNT(*) as count
    FROM coin_inventory
    WHERE user_id = $1
      AND fetched_by IS NULL
    GROUP BY coin_category;
```

Light query. Hits the partial index. No locking needed (read-only).

### 3.4 Purge Stale (Unfetched Expiry)

**When:** Scheduled cron — daily.
**Purpose:** Delete keys nobody ever fetched after 30 days. They're stale — if Bob minted them a month ago and nobody downloaded them, they're likely superseded by newer keys.

```
Input:  max_age_days (default 30)
Output: count of deleted rows

SQL:
    DELETE FROM coin_inventory
    WHERE uploaded_at < NOW() - INTERVAL '1 day' * $1
      AND fetched_by IS NULL
    RETURNING record_id;

    -- RETURNING lets you count how many were deleted
```

### 3.5 Hard Delete (Fetched Cleanup)

**When:** Scheduled cron — hourly.
**Purpose:** After a key is fetched (soft-deleted via `fetched_by = alice`), keep it briefly for audit, then hard-delete to reclaim storage.

```
Input:  grace_hours (default 1)
Output: count of deleted rows

SQL:
    DELETE FROM coin_inventory
    WHERE fetched_by IS NOT NULL
      AND fetched_at < NOW() - INTERVAL '1 hour' * $1
    RETURNING record_id;
```

---

## Part 4: Python Implementation Structure

### 4.1 File: `aqm_server/db.py` — Connection Management

```
Responsibilities:
  - Create asyncpg connection pool at app startup
  - Provide pool to all other modules
  - Health check (SELECT 1)
  - Graceful shutdown (close pool)

Functions:

    create_pool(dsn: str, min_size: int = 5, max_size: int = 20) -> asyncpg.Pool
        dsn format: "postgresql://user:pass@host:5432/aqm"
        Creates connection pool. Call once at startup.
        Raises: ServerDatabaseError if connection fails.

    get_pool() -> asyncpg.Pool
        Returns the singleton pool. Raises if not initialized.

    close_pool() -> None
        Graceful shutdown. Call on app exit.

    health_check(pool: asyncpg.Pool) -> bool
        Runs "SELECT 1". Returns True if connected.
```

### 4.2 File: `aqm_server/coin_inventory.py` — Core Operations

```
class CoinInventoryServer:

    __init__(self, pool: asyncpg.Pool)
        Stores pool reference. All methods acquire connections from pool.

    ── Write ──

    upload_coins(
        user_id:  UUID,
        coins:    list[CoinUpload]
    ) -> int
        Batch INSERT with ON CONFLICT DO NOTHING.
        Returns count of actually inserted rows (excludes skipped duplicates).
        Uses: pool.executemany() for efficiency.

    ── Read + Claim ──

    fetch_coins(
        target_user_id: UUID,
        requester_id:   UUID,
        coin_category:  str,
        count:          int
    ) -> list[CoinRecord]
        The critical CTE query with FOR UPDATE SKIP LOCKED.
        Returns list of CoinRecord (key_id, public_key_blob, signature_blob).
        May return fewer than `count` if insufficient keys available.

    get_inventory_count(
        user_id: UUID
    ) -> dict[str, int]
        GROUP BY coin_category count of unfetched keys.
        Returns {"GOLD": n, "SILVER": n, "BRONZE": n}.

    ── Maintenance ──

    purge_stale(
        max_age_days: int = 30
    ) -> int
        Deletes unfetched keys older than threshold.
        Returns count deleted.

    hard_delete_fetched(
        grace_hours: int = 1
    ) -> int
        Deletes soft-deleted rows past grace period.
        Returns count deleted.
```

### 4.3 File: `aqm_server/api.py` — HTTP Endpoints

```
Framework: FastAPI (you already have it in your conda env)

POST /v1/coins/upload
    Body: { user_id: str, coins: [{key_id, coin_category, public_key_b64, signature_b64}] }
    Note: HTTP transport uses base64 for binary blobs.
          API layer decodes b64 → bytes before passing to coin_inventory.
    Response: { inserted: int }

GET /v1/coins/fetch
    Params: target_user_id, requester_id, coin_category, count
    Response: { coins: [{key_id, coin_category, public_key_b64, signature_b64}] }

GET /v1/coins/count
    Params: user_id
    Response: { gold: int, silver: int, bronze: int }

── Internal / Admin ──

POST /v1/admin/purge-stale
    Body: { max_age_days: int }
    Response: { deleted: int }

POST /v1/admin/hard-delete
    Body: { grace_hours: int }
    Response: { deleted: int }

GET /v1/health
    Response: { status: "ok", db_connected: bool }
```

### 4.4 File: `aqm_server/types.py` — Server-Specific Types

```
@dataclass
class CoinUpload:
    key_id:          str
    coin_category:   str
    public_key_blob: bytes
    signature_blob:  bytes

@dataclass
class CoinRecord:
    key_id:          str
    coin_category:   str
    public_key_blob: bytes
    signature_blob:  bytes

@dataclass
class InventoryCount:
    gold:   int
    silver: int
    bronze: int
```

### 4.5 File: `aqm_server/errors.py`

```
class ServerDatabaseError(Exception)
    Base class.

class ConnectionPoolError(ServerDatabaseError)
    Pool creation or health check failed.

class UploadError(ServerDatabaseError)
    Batch insert failed (not duplicate — actual DB error).

class FetchError(ServerDatabaseError)
    Delete-on-Fetch transaction failed.

class InvalidCoinCategoryError(ServerDatabaseError)
    Validation failed on input.
```

### 4.6 File: `aqm_server/scheduler.py` — Background Jobs

```
Responsibilities:
  - Run purge_stale() daily
  - Run hard_delete_fetched() hourly
  - Log results

Options:
  A. APScheduler (Python library) — simple, in-process
  B. pg_cron (PostgreSQL extension) — runs inside DB, no Python needed
  C. External cron + HTTP call to admin endpoints

Recommended: APScheduler for dev, pg_cron for production.
```

### 4.7 Complete File Tree

```
aqm_server/
├── __init__.py
├── db.py                  ← Connection pool management (4 functions)
├── coin_inventory.py      ← Core CRUD operations (5 functions)
├── api.py                 ← FastAPI endpoints (5 routes)
├── types.py               ← CoinUpload, CoinRecord, InventoryCount
├── errors.py              ← Server-specific exceptions
├── scheduler.py           ← Background purge/delete jobs
├── config.py              ← DSN, pool sizes, purge intervals
├── migrations/
│   ├── 001_create_coin_inventory.sql
│   └── rollback/
│       └── 001_rollback.sql
└── tests/
    ├── conftest.py        ← Test DB setup, pool fixture, table truncation
    ├── test_upload.py
    ├── test_fetch.py      ← Concurrency stress tests live here
    ├── test_purge.py
    └── test_api.py
```

---

## Part 5: asyncpg Patterns You'll Use

### 5.1 Pool Lifecycle

```python
# Startup
pool = await asyncpg.create_pool(
    dsn="postgresql://aqm_user:password@localhost:5432/aqm",
    min_size=5,     # always keep 5 connections warm
    max_size=20,    # scale up to 20 under load
)

# Usage (auto-acquires and releases connection)
async with pool.acquire() as conn:
    rows = await conn.fetch("SELECT * FROM coin_inventory WHERE user_id = $1", bob_id)

# Shutdown
await pool.close()
```

### 5.2 Parameterized Queries

```python
# ALWAYS use $1, $2 placeholders. NEVER string format.
# This prevents SQL injection.

# ✅ Correct
await conn.fetch("SELECT * FROM coin_inventory WHERE user_id = $1", user_id)

# ❌ NEVER do this
await conn.fetch(f"SELECT * FROM coin_inventory WHERE user_id = '{user_id}'")
```

### 5.3 Transactions

```python
# Explicit transaction block
async with conn.transaction():
    # everything inside here is atomic
    rows = await conn.fetch("SELECT ... FOR UPDATE SKIP LOCKED")
    await conn.execute("UPDATE ... SET fetched_by = $1", alice_id)
    # COMMIT happens automatically when block exits
    # ROLLBACK happens automatically on exception
```

### 5.4 Batch Insert

```python
# executemany: one round-trip for N inserts
await conn.executemany(
    """
    INSERT INTO coin_inventory (user_id, key_id, coin_category, public_key_blob, signature_blob)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id, key_id) DO NOTHING
    """,
    [
        (user_id, coin.key_id, coin.coin_category, coin.public_key_blob, coin.signature_blob)
        for coin in coins
    ]
)
```

### 5.5 Fetch Patterns

```python
# Single row
row = await conn.fetchrow("SELECT ... WHERE record_id = $1", rid)
# row["key_id"], row["public_key_blob"], etc.
# Returns None if no match.

# Multiple rows
rows = await conn.fetch("SELECT ... WHERE user_id = $1", uid)
# rows is a list of Record objects
for row in rows:
    print(row["key_id"])

# Single value
count = await conn.fetchval("SELECT COUNT(*) FROM coin_inventory WHERE ...")
# Returns the scalar directly: int, str, etc.
```

---

## Part 6: Implementation Order

```
Step 1:  Docker setup
           docker-compose.yml with PostgreSQL 16
           Connect from psql CLI, verify it works

Step 2:  migrations/001_create_coin_inventory.sql
           Run manually: psql -f 001_create_coin_inventory.sql
           Verify: \dt, \di (list tables, indexes)

Step 3:  db.py — create_pool, close_pool, health_check
           Test: create pool → health check → close pool

Step 4:  coin_inventory.py — upload_coins
           Test: upload 10 coins → SELECT * → verify 10 rows
           Test: upload same 10 again → verify still 10 (ON CONFLICT)

Step 5:  coin_inventory.py — fetch_coins (the big one)
           Test: upload 5 → fetch 3 → verify 3 returned, 2 still available
           Test: fetch 10 when only 5 exist → verify 5 returned, no error

Step 6:  Concurrency test for fetch_coins
           Launch 20 async tasks all fetching 1 key from same user
           Assert: each key returned exactly once, zero duplicates

Step 7:  coin_inventory.py — get_inventory_count
           Test: upload mix of GOLD/SILVER/BRONZE → verify counts match

Step 8:  coin_inventory.py — purge_stale + hard_delete_fetched
           Test: insert with old uploaded_at → purge → verify deleted
           Test: fetch (sets fetched_at) → hard_delete → verify deleted

Step 9:  api.py — all 5 endpoints
           Test with httpx or FastAPI TestClient

Step 10: scheduler.py — wire purge + hard_delete to APScheduler

Step 11: Load test — 10K concurrent fetches, measure p99 latency
```

---

## Part 7: Docker Setup

```yaml
# docker-compose.yml (add to your existing one with Redis)
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  postgres:
    image: postgres:16-alpine
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: aqm
      POSTGRES_USER: aqm_user
      POSTGRES_PASSWORD: aqm_dev_password
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./aqm_server/migrations:/docker-entrypoint-initdb.d  # auto-runs .sql on first start

volumes:
  pgdata:
```

```bash
# Start everything
docker-compose up -d

# Connect from CLI
psql -h localhost -U aqm_user -d aqm

# Verify
\dt              -- list tables
\di              -- list indexes
\d coin_inventory -- describe table
```

---

## Part 8: Testing Strategy

### 8.1 Test Database Setup

```python
# tests/conftest.py

TEST_DSN = "postgresql://aqm_user:aqm_dev_password@localhost:5432/aqm_test"

@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=10)
    # clean slate before each test
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE coin_inventory RESTART IDENTITY")
    yield pool
    await pool.close()

@pytest.fixture
async def inventory(pool):
    return CoinInventoryServer(pool)

@pytest.fixture
def sample_coins():
    return [
        CoinUpload("key-001", "GOLD", os.urandom(1184), os.urandom(2420)),
        CoinUpload("key-002", "SILVER", os.urandom(1184), os.urandom(64)),
        CoinUpload("key-003", "BRONZE", os.urandom(32), os.urandom(64)),
    ]
```

### 8.2 Critical Test: No Duplicate Fetch

```
test_concurrent_fetch_no_duplicates:
    1. Upload 20 Silver keys for Bob
    2. Launch 20 async tasks, each calling fetch_coins(bob, SILVER, count=1)
    3. Collect all returned key_ids into a set
    4. Assert: set size == 20 (every key returned exactly once)
    5. Assert: get_inventory_count(bob) returns SILVER=0 (all claimed)
```

This is the single most important test on the server. If this fails, the system is broken.

### 8.3 Test List

```
test_upload.py:
  test_upload_single_coin
  test_upload_batch
  test_upload_duplicate_idempotent
  test_upload_invalid_category_rejected
  test_upload_empty_list

test_fetch.py:
  test_fetch_returns_correct_tier
  test_fetch_fifo_order
  test_fetch_partial_when_insufficient
  test_fetch_empty_returns_empty_list
  test_fetch_marks_fetched_by
  test_fetch_skips_already_fetched
  test_concurrent_fetch_no_duplicates      ← THE critical test
  test_concurrent_fetch_different_tiers

test_purge.py:
  test_purge_stale_removes_old_unfetched
  test_purge_stale_keeps_recent
  test_purge_stale_ignores_fetched
  test_hard_delete_removes_old_fetched
  test_hard_delete_keeps_recent_fetched

test_api.py:
  test_upload_endpoint
  test_fetch_endpoint
  test_count_endpoint
  test_health_endpoint
  test_invalid_input_422
```

---

## Part 9: Production Considerations

### 9.1 Connection Pooling Tuning

```python
# Dev
pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

# Production
pool = await asyncpg.create_pool(
    dsn,
    min_size=10,
    max_size=50,
    max_inactive_connection_lifetime=300,  # close idle connections after 5 min
    command_timeout=10,                     # query timeout: 10 seconds
)
```

### 9.2 Monitoring Queries

```sql
-- How many unfetched keys per user (top 10)?
SELECT user_id, COUNT(*) as available
FROM coin_inventory
WHERE fetched_by IS NULL
GROUP BY user_id
ORDER BY available DESC
LIMIT 10;

-- How many keys fetched in last hour?
SELECT COUNT(*) FROM coin_inventory
WHERE fetched_at > NOW() - INTERVAL '1 hour';

-- Table size on disk?
SELECT pg_size_pretty(pg_total_relation_size('coin_inventory'));

-- Index sizes?
SELECT indexname, pg_size_pretty(pg_relation_size(indexname::regclass))
FROM pg_indexes
WHERE tablename = 'coin_inventory';

-- Slow queries (enable pg_stat_statements extension)?
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
```

### 9.3 Scaling Path

```
Phase 1 (now):      Single PostgreSQL, asyncpg pool
                     Handles ~10K concurrent users easily

Phase 2 (growth):   Add read replica for get_inventory_count
                     PgBouncer in front for connection pooling
                     Partitioning by user_id if table gets huge

Phase 3 (scale):    Migrate to CockroachDB (PostgreSQL-compatible)
                     Schema nearly identical, swap BIGSERIAL → UUID
                     Auto-sharding across nodes
```
