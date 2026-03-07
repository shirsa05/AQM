# AQM Client Database — Complete Architecture & API Guide

## Scope

This document covers the two local Redis databases running on each device:

1. **Secure Vault** — stores YOUR private keys (you are the receiver)
2. **Smart Inventory** — caches OTHER people's public keys (you are the sender)

Every device runs BOTH databases simultaneously. When you mint keys, your private halves go into your Vault. When you pre-fetch someone else's keys, their public halves go into your Inventory.

---

## Part 1: Redis Data Model

### 1.1 Key Naming Convention

All keys follow a strict namespace pattern to avoid collisions and enable pattern scanning:

```
{db}:{version}:{entity}:{id}:{qualifier}

Examples:
  vault:v1:key:A7F3B201              → Hash (one private key entry)
  inv:v1:key:bob_uuid:A7F3B201       → Hash (one cached public key)
  inv:v1:idx:bob_uuid:GOLD           → Sorted Set (index for coin selection)
  inv:v1:meta:bob_uuid               → Hash (contact metadata)
  vault:v1:stats                     → Hash (aggregate counters)
```

**Why versioned prefixes?** When schema changes, you deploy v2 keys alongside v1 and migrate in background. No downtime.

### 1.2 Data Structures Used

| Redis Type | Where Used | Why This Type |
|------------|-----------|---------------|
| **Hash** | Individual key entries (Vault + Inventory) | Groups related fields under one key, O(1) field access |
| **Sorted Set** | Inventory coin indexes | Ordered by timestamp → `ZPOPMIN` gives oldest coin in O(log N), perfect for FIFO consumption |
| **Hash** | Contact metadata | Stores priority, last contact time, key counts per tier |
| **Hash** | Stats/counters | Atomic `HINCRBY` for tracking mints, burns, fetches |

---

## Part 2: Secure Vault

### 2.1 Purpose

Holds the private half of every coin this device has minted. When an encrypted parcel arrives referencing Key ID `#A7F3B201`, the Vault looks it up, hands the encrypted blob to the Crypto Wrapper (which decrypts it with the hardware key), and then **burns** the entry so it can never be reused.

### 2.2 Entry Lifecycle

```
MINTED ──store()──▶ ACTIVE ──burn()──▶ BURNED ──(TTL/purge)──▶ DELETED
                      │
                      │ (30 days unused)
                      ▼
                   EXPIRED ──purge()──▶ DELETED
```

### 2.3 Redis Schema: Single Vault Entry

```
Key:    vault:v1:key:{key_id}
Type:   Hash
TTL:    30 days (set on creation)

Fields:
  coin_category   : str    — "GOLD" | "SILVER" | "BRONZE"
  encrypted_blob  : bytes  — Private key encrypted by hardware AES key
  encryption_iv   : bytes  — 12-byte AES-GCM nonce
  auth_tag        : bytes  — 16-byte AES-GCM tag
  status          : str    — "ACTIVE" | "BURNED"
  created_at      : int    — Unix timestamp (ms)
  coin_version    : str    — Algorithm version (e.g., "kyber768_v1")
```

### 2.4 Redis Schema: Vault Stats

```
Key:    vault:v1:stats
Type:   Hash

Fields:
  active_gold     : int
  active_silver   : int
  active_bronze   : int
  total_burned    : int
  total_expired   : int
```

Updated atomically via `HINCRBY` on every store/burn/purge.

### 2.5 Function Signatures & Behavior

---

#### `store_key`

```
store_key(
    key_id:         str,
    coin_category:  Literal["GOLD", "SILVER", "BRONZE"],
    encrypted_blob: bytes,
    encryption_iv:  bytes,
    auth_tag:       bytes,
    coin_version:   str = "kyber768_v1"
) -> bool
```

**What it does:**
- Writes a new Hash at `vault:v1:key:{key_id}` with status `ACTIVE`
- Sets TTL of 30 days on the key
- Increments the appropriate counter in `vault:v1:stats`
- Returns `False` if `key_id` already exists (duplicate protection)

**Called by:** Coin Minting module, during Phase 1 (Asynchronous Minting — device charging/WiFi)

**Failure modes:** Duplicate key_id → reject. Redis down → raise, let caller retry.

---

#### `fetch_key`

```
fetch_key(
    key_id: str
) -> Optional[VaultEntry]
```

**What it does:**
- Reads `HGETALL` on `vault:v1:key:{key_id}`
- Returns `None` if key doesn't exist OR `status == BURNED`
- Does NOT delete or modify the entry (read-only)

**Called by:** Message Receiver (Phase 3), when a parcel arrives with a Coin ID

**Returns:** `VaultEntry` dataclass with all fields, or `None`

---

#### `burn_key`

```
burn_key(
    key_id: str
) -> bool
```

**What it does (two-phase atomic burn):**
1. **Immediate:** Sets `status = BURNED` via `HSET` — any concurrent `fetch_key` call now returns `None`
2. **Deferred:** Reduces TTL to 60 seconds (`EXPIRE key 60`) — gives time for network retransmissions, then auto-deletes
3. Decrements active counter, increments burned counter in stats

**Why two-phase?** If Alice's parcel arrives twice due to network retry, the second arrival sees `BURNED` and is dropped. But we keep the entry briefly so the app can log/audit the duplicate.

**Called by:** Crypto Wrapper, immediately after successful decapsulation

**Returns:** `False` if key_id not found or already burned

---

#### `count_active`

```
count_active(
    coin_category: Optional[str] = None
) -> dict[str, int] | int
```

**What it does:**
- If `coin_category` given: reads single field from `vault:v1:stats` → returns int
- If `None`: reads all three active counters → returns `{"GOLD": n, "SILVER": n, "BRONZE": n}`

**Called by:** Minting scheduler — decides whether to mint more coins based on current stock levels

**Why stats hash instead of SCAN?** SCAN over all keys is O(N) and slow. Maintaining counters via `HINCRBY` is O(1). The tradeoff is you must keep counters in sync — every `store`, `burn`, and `purge` must update stats atomically.

---

#### `purge_expired`

```
purge_expired(
    max_age_days: int = 30
) -> int
```

**What it does:**
- SCAN all `vault:v1:key:*` entries
- For each: check `created_at` against current time
- Delete entries older than `max_age_days` that are still `ACTIVE` (unused keys that were never consumed)
- Update stats counters
- Returns count of purged keys

**Called by:** Background scheduler (e.g., daily cron when device is charging)

**Note:** This is a safety net. Redis TTL should handle most expiry automatically. This catches edge cases where TTL was lost (e.g., after a `PERSIST` call or Redis restore from backup).

---

#### `exists`

```
exists(
    key_id: str
) -> bool
```

**What it does:** `EXISTS vault:v1:key:{key_id}` — simple existence check, doesn't load data.

**Called by:** Coin Minting module before storing (duplicate guard), Message Receiver before full fetch (fast-path rejection of unknown key IDs).

---

#### `get_all_active_ids`

```
get_all_active_ids(
    coin_category: Optional[str] = None
) -> list[str]
```

**What it does:**
- SCAN `vault:v1:key:*`, filter by `status == ACTIVE` and optionally `coin_category`
- Returns list of key_ids

**Called by:** Server upload module — needs to know which key_ids to advertise. Also used by the Minting module to check what's already minted.

**Performance note:** This is O(N) — only call during background sync, never in the message hot path.

---

### 2.6 Vault Transaction Patterns

**Store (must be atomic — no partial writes):**
```
MULTI
  HSET vault:v1:key:{id} coin_category ... status ACTIVE created_at ...
  EXPIRE vault:v1:key:{id} 2592000
  HINCRBY vault:v1:stats active_{tier} 1
EXEC
```

**Burn (must be atomic — status flip + stats update):**
```
MULTI
  HSET vault:v1:key:{id} status BURNED
  EXPIRE vault:v1:key:{id} 60
  HINCRBY vault:v1:stats active_{tier} -1
  HINCRBY vault:v1:stats total_burned 1
EXEC
```

---

## Part 3: Smart Inventory

### 3.1 Purpose

Caches public keys for contacts so that sending a message requires zero network round-trips. Organized by contact and coin tier, with strict per-contact budget caps.

### 3.2 Entry Lifecycle

```
SERVER ──fetch──▶ CACHED ──select_coin()──▶ CONSUMED ──▶ DELETED
                    │
                    │ (30 days unused / GC)
                    ▼
                  STALE ──garbage_collect()──▶ DELETED
```

### 3.3 Redis Schema: Single Inventory Entry

```
Key:    inv:v1:key:{contact_id}:{key_id}
Type:   Hash
TTL:    None (managed by GC, not Redis TTL — because budget logic needs control)

Fields:
  coin_category    : str    — "GOLD" | "SILVER" | "BRONZE"
  public_key       : bytes  — Kyber-768 pk (1184B) or X25519 pk (32B)
  signature        : bytes  — Dilithium sig (2420B) or Ed25519 sig (64B)
  fetched_at       : int    — Unix timestamp (ms) when downloaded from server
```

### 3.4 Redis Schema: Coin Index (for fast selection)

```
Key:    inv:v1:idx:{contact_id}:{coin_category}
Type:   Sorted Set

Members: key_id strings
Score:   fetched_at timestamp

Purpose: ZPOPMIN gives the oldest coin → FIFO consumption
         ZCARD gives count → budget enforcement
```

### 3.5 Redis Schema: Contact Metadata

```
Key:    inv:v1:meta:{contact_id}
Type:   Hash

Fields:
  priority       : str  — "BESTIE" | "MATE" | "STRANGER"
  last_msg_at    : int  — Unix timestamp of last message sent to this contact
  display_name   : str  — For logging/debugging only
```

### 3.6 Budget Caps (Constants)

```
BUDGET = {
    "BESTIE": {"GOLD": 5, "SILVER": 4, "BRONZE": 1},
    "MATE":   {"GOLD": 0, "SILVER": 6, "BRONZE": 4},
    "STRANGER": {"GOLD": 0, "SILVER": 0, "BRONZE": 0},
}
```

Strangers have zero pre-fetched keys. All keys are fetched on-demand.

### 3.7 Function Signatures & Behavior

---

#### `store_key`

```
store_key(
    contact_id:     str,
    key_id:         str,
    coin_category:  Literal["GOLD", "SILVER", "BRONZE"],
    public_key:     bytes,
    signature:      bytes
) -> bool
```

**What it does:**
1. Reads `priority` from `inv:v1:meta:{contact_id}`
2. Reads current count via `ZCARD inv:v1:idx:{contact_id}:{coin_category}`
3. Checks against `BUDGET[priority][coin_category]`
4. If under cap: writes Hash + adds to Sorted Set index (atomic via MULTI)
5. If at cap: returns `False`

**Called by:** Pre-fetch scheduler (Phase 2) after downloading keys from server

**Edge case:** If contact has no metadata yet, treat as `STRANGER` → reject all stores (they should be fetched on-demand).

---

#### `select_coin`

```
select_coin(
    contact_id:   str,
    desired_tier: Literal["GOLD", "SILVER", "BRONZE"]
) -> Optional[InventoryEntry]
```

**What it does:**
1. `ZPOPMIN inv:v1:idx:{contact_id}:{desired_tier}` — atomically pops oldest key_id
2. If found: `HGETALL inv:v1:key:{contact_id}:{key_id}` → build entry → `DEL` the hash → return entry
3. If empty: try fallback tier (GOLD→SILVER→BRONZE)
4. If all empty: return `None` (signals caller to do on-demand server fetch)

**Fallback order:**
- Requested GOLD → try SILVER → try BRONZE → None
- Requested SILVER → try BRONZE → None
- Requested BRONZE → None (no upward fallback — never waste a Gold when Bronze was requested)

**Called by:** Message Sender (Phase 3). The Context Manager has already decided the tier based on battery/signal. Your job is just to hand over the best available key.

**Critical:** This is the hot path. Must complete in < 2ms. `ZPOPMIN` is O(log N), hash ops are O(1). Total: fast.

---

#### `consume_key`

```
consume_key(
    contact_id: str,
    key_id:     str
) -> bool
```

**What it does:**
- Deletes `inv:v1:key:{contact_id}:{key_id}`
- Removes `key_id` from the sorted set index
- Updates `last_msg_at` on contact metadata

**Called by:** Internally by `select_coin`, or externally if the caller needs to manually discard a key (e.g., signature verification failed after selection).

**Note:** If `select_coin` is the only consumer, you can inline this logic there. Exposing it separately is useful for error recovery.

---

#### `get_inventory`

```
get_inventory(
    contact_id: Optional[str] = None
) -> dict
```

**What it does:**
- If `contact_id` given: returns `{"GOLD": n, "SILVER": n, "BRONZE": n}` via `ZCARD` on each index
- If `None`: iterates all contacts, returns `{contact_id: {GOLD: n, SILVER: n, BRONZE: n}, ...}`

**Called by:** UI (shows "5 🟡 4 ⚪ 1 🟤 keys cached for Bob"), Pre-fetch scheduler (decides what to replenish), Minting scheduler on remote side (server tells Bob "Alice is low on your keys, mint more").

---

#### `set_contact_priority`

```
set_contact_priority(
    contact_id: str,
    priority:   Literal["BESTIE", "MATE", "STRANGER"]
) -> bool
```

**What it does:**
1. Updates `priority` field in `inv:v1:meta:{contact_id}`
2. If downgraded (e.g., BESTIE → MATE): trims excess keys
   - MATE has 0 Gold cap → evict all Gold keys for this contact
   - MATE has 6 Silver cap → if currently 4, no trim needed
3. If upgraded (e.g., MATE → BESTIE): does NOT auto-fetch — just raises the cap. Pre-fetch scheduler fills the gap on next run.

**Called by:** Contact frequency analyzer (background module that tracks message patterns and reclassifies contacts).

**Trim logic:** Pop excess from sorted set, delete corresponding hashes. Start with newest keys (they have more TTL remaining — wasteful but simpler than LRU here since counts are small).

---

#### `garbage_collect`

```
garbage_collect(
    inactive_days: int = 30
) -> GCResult
```

**What it does:**
1. SCAN all `inv:v1:meta:*` entries
2. For each contact: check `last_msg_at`
3. If `last_msg_at` older than `inactive_days`:
   - Delete ALL keys for that contact (hashes + sorted sets)
   - Downgrade contact to STRANGER
   - Optionally delete metadata entirely
4. Returns `GCResult(contacts_cleaned: int, keys_deleted: int, bytes_freed: int)`

**Called by:** Background scheduler (daily, when device is charging)

---

#### `register_contact`

```
register_contact(
    contact_id:   str,
    priority:     Literal["BESTIE", "MATE", "STRANGER"],
    display_name: str = ""
) -> bool
```

**What it does:** Creates `inv:v1:meta:{contact_id}` with initial priority and `last_msg_at = now`. Must be called before `store_key` works for a contact (no metadata = treated as STRANGER = zero budget).

**Called by:** Contact management module when user adds a contact or when frequency analyzer first classifies someone.

---

#### `get_storage_usage`

```
get_storage_usage() -> StorageReport
```

**What it does:**
- Calculates total bytes used by inventory across all contacts
- Breaks down by contact and tier
- Compares against device storage budget (configurable, e.g., 64 KB max for all cached keys)

**Returns:**
```
StorageReport(
    total_bytes:    int,
    per_contact:    dict[str, int],
    budget_bytes:   int,
    utilization_pct: float
)
```

**Called by:** UI, and by the pre-fetch scheduler to decide if there's room for more keys.

---

### 3.8 Inventory Transaction Patterns

**Store (atomic budget check + write):**
```
WATCH inv:v1:idx:{contact_id}:{tier}
count = ZCARD inv:v1:idx:{contact_id}:{tier}
if count >= BUDGET[priority][tier]: UNWATCH, return False
MULTI
  HSET inv:v1:key:{contact_id}:{key_id} ...
  ZADD inv:v1:idx:{contact_id}:{tier} {fetched_at} {key_id}
EXEC
```

**Select (atomic pop + read + delete):**
```
key_id = ZPOPMIN inv:v1:idx:{contact_id}:{tier}
if key_id:
  entry = HGETALL inv:v1:key:{contact_id}:{key_id}
  DEL inv:v1:key:{contact_id}:{key_id}
  HSET inv:v1:meta:{contact_id} last_msg_at {now}
  return entry
else:
  # try fallback tier...
```

---

## Part 4: Shared Infrastructure

### 4.1 Connection Management

```
create_redis_client(
    host: str = "localhost",
    port: int = 6379,
    db: int = 0,               # Use db=0 for Vault, db=1 for Inventory
    decode_responses: bool = False  # Keep False — you store binary blobs
) -> Redis
```

**Use separate Redis logical databases** (db 0 vs db 1) to isolate Vault from Inventory. This lets you `FLUSHDB` one without nuking the other. If running Redis in production on a phone, this will be a single embedded Redis instance with two logical DBs.

### 4.2 Data Classes (Shared Types)

```
@dataclass
class VaultEntry:
    key_id:         str
    coin_category:  str        # "GOLD" | "SILVER" | "BRONZE"
    encrypted_blob: bytes
    encryption_iv:  bytes
    auth_tag:       bytes
    status:         str        # "ACTIVE" | "BURNED"
    created_at:     int
    coin_version:   str

@dataclass
class InventoryEntry:
    contact_id:     str
    key_id:         str
    coin_category:  str
    public_key:     bytes
    signature:      bytes
    fetched_at:     int

@dataclass
class ContactMeta:
    contact_id:     str
    priority:       str        # "BESTIE" | "MATE" | "STRANGER"
    last_msg_at:    int
    display_name:   str

@dataclass
class StorageReport:
    total_bytes:     int
    per_contact:     dict[str, int]
    budget_bytes:    int
    utilization_pct: float

@dataclass
class GCResult:
    contacts_cleaned: int
    keys_deleted:     int
    bytes_freed:      int
```

### 4.3 Error Handling Strategy

| Error | Response |
|-------|----------|
| Redis connection refused | Raise `VaultUnavailableError` — caller must retry or queue the operation |
| Key not found on fetch | Return `None` — not an error, just means key was burned/expired |
| Budget exceeded on store | Return `False` — caller decides whether to evict or skip |
| WATCH/MULTI conflict (optimistic lock failure) | Retry up to 3 times, then raise `ConcurrencyError` |
| Corrupted blob (wrong byte length) | Log + delete entry + return `None` — don't serve bad data |

### 4.4 Logging Events

Every function should emit a structured log entry for debugging and audit:

| Event | Severity | Key Fields |
|-------|----------|------------|
| `key_stored` | INFO | key_id, coin_category |
| `key_fetched` | DEBUG | key_id |
| `key_burned` | INFO | key_id, time_alive_ms |
| `key_expired` | WARN | key_id, age_days |
| `budget_exceeded` | WARN | contact_id, tier, current_count, cap |
| `coin_selected` | INFO | contact_id, requested_tier, actual_tier |
| `fallback_used` | WARN | contact_id, requested_tier, fallback_tier |
| `gc_completed` | INFO | contacts_cleaned, keys_deleted, bytes_freed |
| `duplicate_rejected` | WARN | key_id |

---

## Part 5: File/Module Structure

```
aqm_db/
├── __init__.py
├── config.py              ← Budget caps, TTLs, Redis connection params
├── types.py               ← VaultEntry, InventoryEntry, ContactMeta, etc.
├── errors.py              ← Custom exceptions
├── connection.py          ← Redis client factory, health check
├── vault.py               ← SecureVault class (all vault operations)
├── inventory.py           ← SmartInventory class (all inventory operations)
├── gc.py                  ← GarbageCollector (runs on scheduler)
├── stats.py               ← StorageReport generation, inventory summaries
└── tests/
    ├── test_vault.py
    ├── test_inventory.py
    ├── test_gc.py
    ├── test_concurrency.py
    └── conftest.py        ← Redis test fixtures (use fakeredis or test DB)
```

---

## Part 6: Implementation Order

Follow this exact sequence. Each step depends on the previous.

```
Step 1:  config.py + types.py + errors.py + connection.py
         (foundation — no Redis logic yet, just types and config)

Step 2:  vault.py → store_key + exists
         (simplest write path, proves Redis connection works)

Step 3:  vault.py → fetch_key + burn_key
         (complete the Vault read/write cycle)

Step 4:  vault.py → count_active + purge_expired + get_all_active_ids
         (Vault is now feature-complete)

Step 5:  test_vault.py → full test suite for Vault
         (lock it down before moving on)

Step 6:  inventory.py → register_contact + store_key
         (Inventory write path with budget enforcement)

Step 7:  inventory.py → select_coin + consume_key
         (the critical hot path)

Step 8:  inventory.py → set_contact_priority + get_inventory
         (contact management)

Step 9:  gc.py → garbage_collect
         (cleanup logic)

Step 10: stats.py → get_storage_usage
         (reporting)

Step 11: test_inventory.py + test_gc.py + test_concurrency.py
         (full test suite)

Step 12: Integration smoke test
         (mint → store in vault → upload to server → fetch → store in inventory → select → send)
```
