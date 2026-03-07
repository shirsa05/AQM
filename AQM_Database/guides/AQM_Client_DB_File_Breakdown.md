# AQM Client Database — File-by-File Module Breakdown

This document specifies exactly what goes in each file, what each file imports, and what it exports. Read alongside `AQM_Client_DB_Guide.md` for the full function behavior docs.

---

## `aqm_db/config.py`

**Purpose:** Single source of truth for all constants, budget caps, TTLs, and Redis connection parameters. No logic, no classes — just values. Every other module imports from here.

```
# ──────────────────────────────────────────────
# Redis Connection
# ──────────────────────────────────────────────

REDIS_HOST              = "localhost"
REDIS_PORT              = 6379
REDIS_VAULT_DB          = 0          # Logical DB for Secure Vault
REDIS_INVENTORY_DB      = 1          # Logical DB for Smart Inventory
REDIS_SOCKET_TIMEOUT    = 5          # seconds
REDIS_RETRY_ATTEMPTS    = 3
REDIS_RETRY_DELAY       = 0.5       # seconds between retries

# ──────────────────────────────────────────────
# Key Namespace Prefixes
# ──────────────────────────────────────────────

VAULT_KEY_PREFIX        = "vault:v1:key"        # vault:v1:key:{key_id}
VAULT_STATS_KEY         = "vault:v1:stats"

INV_KEY_PREFIX          = "inv:v1:key"           # inv:v1:key:{contact_id}:{key_id}
INV_IDX_PREFIX          = "inv:v1:idx"           # inv:v1:idx:{contact_id}:{coin_category}
INV_META_PREFIX         = "inv:v1:meta"          # inv:v1:meta:{contact_id}

# ──────────────────────────────────────────────
# Vault Settings
# ──────────────────────────────────────────────

VAULT_KEY_TTL_SECONDS       = 2_592_000     # 30 days
VAULT_BURN_GRACE_SECONDS    = 60            # keep burned key for 60s before hard delete
VAULT_PURGE_MAX_AGE_DAYS    = 30

# ──────────────────────────────────────────────
# Inventory Budget Caps (from AQM paper Table 2)
# ──────────────────────────────────────────────

BUDGET_CAPS = {
    "BESTIE":   {"GOLD": 5, "SILVER": 4, "BRONZE": 1},
    "MATE":     {"GOLD": 0, "SILVER": 6, "BRONZE": 4},
    "STRANGER": {"GOLD": 0, "SILVER": 0, "BRONZE": 0},
}

# ──────────────────────────────────────────────
# Inventory Settings
# ──────────────────────────────────────────────

INV_GC_INACTIVE_DAYS        = 30
INV_MAX_STORAGE_BYTES       = 65_536        # 64 KB total budget for all cached keys
INV_OPTIMISTIC_LOCK_RETRIES = 3

# ──────────────────────────────────────────────
# Coin Tier Fallback Order
# ──────────────────────────────────────────────

TIER_FALLBACK = {
    "GOLD":   ["SILVER", "BRONZE"],
    "SILVER": ["BRONZE"],
    "BRONZE": [],
}

# ──────────────────────────────────────────────
# Valid Enums (for validation)
# ──────────────────────────────────────────────

VALID_COIN_CATEGORIES   = {"GOLD", "SILVER", "BRONZE"}
VALID_PRIORITIES        = {"BESTIE", "MATE", "STRANGER"}
VALID_STATUSES          = {"ACTIVE", "BURNED"}

# ──────────────────────────────────────────────
# Approximate Blob Sizes (bytes, for storage calc)
# ──────────────────────────────────────────────

COIN_SIZE_BYTES = {
    "GOLD":   3_604,    # Kyber-768 pk (1184) + Dilithium sig (2420)
    "SILVER": 1_248,    # Kyber-768 pk (1184) + Ed25519 sig (64)
    "BRONZE": 96,       # X25519 pk (32) + Ed25519 sig (64)
}
```

**Imports:** Nothing (leaf module)
**Imported by:** Every other module

---

## `aqm_db/types.py`

**Purpose:** All dataclasses and type aliases shared across the project. These are the contracts your teammates code against. No logic whatsoever.

```
Contains:

@dataclass VaultEntry
    key_id:         str
    coin_category:  str
    encrypted_blob: bytes
    encryption_iv:  bytes
    auth_tag:       bytes
    status:         str
    created_at:     int
    coin_version:   str

@dataclass InventoryEntry
    contact_id:     str
    key_id:         str
    coin_category:  str
    public_key:     bytes
    signature:      bytes
    fetched_at:     int

@dataclass ContactMeta
    contact_id:     str
    priority:       str
    last_msg_at:    int
    display_name:   str

@dataclass StorageReport
    total_bytes:     int
    per_contact:     dict[str, int]
    budget_bytes:    int
    utilization_pct: float

@dataclass GCResult
    contacts_cleaned: int
    keys_deleted:     int
    bytes_freed:      int

@dataclass VaultStats
    active_gold:    int
    active_silver:  int
    active_bronze:  int
    total_burned:   int
    total_expired:  int

@dataclass InventorySummary
    contact_id:  str
    gold_count:  int
    silver_count: int
    bronze_count: int
    priority:    str

@dataclass HealthStatus
    vault_connected:     bool
    inventory_connected: bool
    vault_key_count:     int
    inventory_key_count: int
    uptime_seconds:      float
```

**Imports:** `dataclasses` (stdlib only)
**Imported by:** Every other module

---

## `aqm_db/errors.py`

**Purpose:** Custom exception hierarchy. Every module raises these instead of raw Redis exceptions — this decouples your teammates from the Redis implementation detail.

```
Contains:

class AQMDatabaseError(Exception)
    Base class for all database errors.

class VaultUnavailableError(AQMDatabaseError)
    Redis connection to Vault DB failed.
    Raised by: connection.py, vault.py

class InventoryUnavailableError(AQMDatabaseError)
    Redis connection to Inventory DB failed.
    Raised by: connection.py, inventory.py

class KeyNotFoundError(AQMDatabaseError)
    Requested key_id does not exist.
    Raised by: vault.py (burn_key when key missing)

class KeyAlreadyExistsError(AQMDatabaseError)
    Attempted to store a duplicate key_id.
    Raised by: vault.py (store_key)

class BudgetExceededError(AQMDatabaseError)
    Coin storage cap reached for this contact+tier.
    Attributes: contact_id, coin_category, current_count, cap
    Raised by: inventory.py (store_key)

class ContactNotRegisteredError(AQMDatabaseError)
    No metadata exists for this contact_id.
    Raised by: inventory.py (store_key, select_coin)

class ConcurrencyError(AQMDatabaseError)
    Optimistic lock (WATCH) failed after max retries.
    Raised by: inventory.py (store_key)

class InvalidCoinCategoryError(AQMDatabaseError)
    coin_category not in {"GOLD", "SILVER", "BRONZE"}.
    Raised by: vault.py, inventory.py (input validation)

class InvalidPriorityError(AQMDatabaseError)
    priority not in {"BESTIE", "MATE", "STRANGER"}.
    Raised by: inventory.py (set_contact_priority, register_contact)

class KeyAlreadyBurnedError(AQMDatabaseError)
    Attempted to burn a key that was already burned.
    Raised by: vault.py (burn_key)
```

**Imports:** Nothing (stdlib only)
**Imported by:** vault.py, inventory.py, gc.py, connection.py

---

## `aqm_db/connection.py`

**Purpose:** Factory for Redis client instances. Handles connection creation, health checks, and reconnection. All other modules receive a Redis client from here — they never construct their own.

```
Contains:

function create_vault_client() -> redis.Redis
    Creates a Redis client connected to REDIS_VAULT_DB (db=0).
    Sets decode_responses=False (binary blobs).
    Sets socket_timeout, retry config from config.py.
    Pings on creation — raises VaultUnavailableError if unreachable.

function create_inventory_client() -> redis.Redis
    Same as above but connects to REDIS_INVENTORY_DB (db=1).
    Raises InventoryUnavailableError if unreachable.

function health_check(vault_client, inventory_client) -> HealthStatus
    Pings both clients.
    Counts keys via DBSIZE.
    Returns HealthStatus dataclass.

function close_all(vault_client, inventory_client) -> None
    Graceful shutdown. Calls .close() on both clients.
    Used during app shutdown / test teardown.
```

**Imports from project:** `config`, `types.HealthStatus`, `errors.VaultUnavailableError`, `errors.InventoryUnavailableError`
**Imports external:** `redis`
**Imported by:** vault.py, inventory.py, tests/conftest.py, app entrypoint

---

## `aqm_db/vault.py`

**Purpose:** The `SecureVault` class. All operations on Bob's private key store. This is the most security-critical file — a bug here means lost messages or replay attacks.

```
class SecureVault:

    __init__(self, client: redis.Redis)
        Stores the Redis client reference.
        Does NOT create its own connection — receives it from connection.py.
        Why: testability. Tests pass in a fakeredis client.

    ─────────────────────────────────────────────
    WRITE OPERATIONS
    ─────────────────────────────────────────────

    store_key(
        self,
        key_id:         str,
        coin_category:  str,
        encrypted_blob: bytes,
        encryption_iv:  bytes,
        auth_tag:       bytes,
        coin_version:   str = "kyber768_v1"
    ) -> bool
        Validates coin_category against VALID_COIN_CATEGORIES.
        Checks EXISTS — raises KeyAlreadyExistsError if duplicate.
        Atomic MULTI/EXEC:
          - HSET the vault entry with status=ACTIVE
          - EXPIRE with VAULT_KEY_TTL_SECONDS
          - HINCRBY stats counter
        Returns True on success.
        Raises: InvalidCoinCategoryError, KeyAlreadyExistsError, VaultUnavailableError

    burn_key(self, key_id: str) -> bool
        Reads current status — raises KeyNotFoundError if missing,
        KeyAlreadyBurnedError if already BURNED.
        Atomic MULTI/EXEC:
          - HSET status=BURNED
          - EXPIRE with VAULT_BURN_GRACE_SECONDS (60s)
          - HINCRBY active counter -1
          - HINCRBY total_burned +1
        Returns True on success.
        Raises: KeyNotFoundError, KeyAlreadyBurnedError, VaultUnavailableError

    ─────────────────────────────────────────────
    READ OPERATIONS
    ─────────────────────────────────────────────

    fetch_key(self, key_id: str) -> Optional[VaultEntry]
        HGETALL on vault:v1:key:{key_id}.
        If not found or status==BURNED: returns None.
        Otherwise: deserializes into VaultEntry and returns.
        Never modifies data.
        Raises: VaultUnavailableError

    exists(self, key_id: str) -> bool
        EXISTS vault:v1:key:{key_id}.
        Pure existence check, no data loaded.
        Raises: VaultUnavailableError

    count_active(self, coin_category: Optional[str] = None) -> dict[str, int] | int
        Reads from vault:v1:stats hash.
        If coin_category given: returns single int.
        If None: returns dict of all three tiers.
        Validates coin_category if provided.
        Raises: InvalidCoinCategoryError, VaultUnavailableError

    get_all_active_ids(self, coin_category: Optional[str] = None) -> list[str]
        SCAN vault:v1:key:* pattern.
        For each match: HMGET status and coin_category.
        Filters to ACTIVE only, optionally filtered by tier.
        Returns list of key_id strings.
        WARNING: O(N) — background use only.
        Raises: InvalidCoinCategoryError, VaultUnavailableError

    ─────────────────────────────────────────────
    MAINTENANCE OPERATIONS
    ─────────────────────────────────────────────

    purge_expired(self, max_age_days: int = 30) -> int
        SCAN vault:v1:key:* pattern.
        For each ACTIVE key: check created_at against now - max_age_days.
        If expired: DEL key, HINCRBY active counter -1, HINCRBY total_expired +1.
        Returns count of purged keys.
        Raises: VaultUnavailableError

    get_stats(self) -> VaultStats
        HGETALL vault:v1:stats.
        Deserializes into VaultStats dataclass.
        Raises: VaultUnavailableError

    ─────────────────────────────────────────────
    INTERNAL HELPERS (private methods)
    ─────────────────────────────────────────────

    _vault_key(self, key_id: str) -> str
        Returns f"{VAULT_KEY_PREFIX}:{key_id}"

    _validate_coin_category(self, coin_category: str) -> None
        Raises InvalidCoinCategoryError if not in VALID_COIN_CATEGORIES

    _serialize_entry(self, ...) -> dict
        Converts function args into the flat dict for HSET.
        Handles bytes→bytes (no encoding needed for Redis).
        Sets created_at to current timestamp.
        Sets status to "ACTIVE".

    _deserialize_entry(self, key_id: str, data: dict) -> VaultEntry
        Converts Redis HGETALL response (dict of bytes) into VaultEntry.
        Decodes string fields, keeps blob fields as bytes.
```

**Imports from project:** `config.*`, `types.VaultEntry`, `types.VaultStats`, `errors.*`
**Imports external:** `redis`, `time`
**Imported by:** app entrypoint, tests/test_vault.py

**Total functions:** 8 public + 3 private = **11 functions**

---

## `aqm_db/inventory.py`

**Purpose:** The `SmartInventory` class. All operations on Alice's cached public key store. The most complex file — it manages budget caps, sorted set indexes, contact metadata, coin selection with fallback, and optimistic locking.

```
class SmartInventory:

    __init__(self, client: redis.Redis)
        Stores the Redis client reference.
        Receives from connection.py (same pattern as Vault).

    ─────────────────────────────────────────────
    CONTACT MANAGEMENT
    ─────────────────────────────────────────────

    register_contact(
        self,
        contact_id:   str,
        priority:     str,
        display_name: str = ""
    ) -> bool
        Validates priority against VALID_PRIORITIES.
        Creates inv:v1:meta:{contact_id} hash with:
          priority, last_msg_at=now, display_name.
        Returns False if contact already registered (idempotent — does not overwrite).
        To change priority, use set_contact_priority().
        Raises: InvalidPriorityError, InventoryUnavailableError

    set_contact_priority(
        self,
        contact_id: str,
        priority:   str
    ) -> bool
        Validates priority.
        Reads current priority from metadata.
        If not registered: raises ContactNotRegisteredError.
        Updates priority field.
        If DOWNGRADED: calls _trim_excess(contact_id, new_priority)
          to evict keys that exceed the new, lower budget cap.
        If UPGRADED: no immediate action (pre-fetch scheduler fills the gap).
        Returns True on success.
        Raises: InvalidPriorityError, ContactNotRegisteredError, InventoryUnavailableError

    get_contact_meta(self, contact_id: str) -> Optional[ContactMeta]
        HGETALL on inv:v1:meta:{contact_id}.
        Returns ContactMeta or None if not registered.
        Raises: InventoryUnavailableError

    ─────────────────────────────────────────────
    WRITE OPERATIONS
    ─────────────────────────────────────────────

    store_key(
        self,
        contact_id:    str,
        key_id:        str,
        coin_category: str,
        public_key:    bytes,
        signature:     bytes
    ) -> bool
        The most complex write in the entire system. Steps:

        1. Validate coin_category.
        2. Read contact metadata — raise ContactNotRegisteredError if missing.
        3. Read priority from metadata.
        4. Look up budget cap: BUDGET_CAPS[priority][coin_category].
        5. If cap is 0: raise BudgetExceededError (MATE can't store GOLD, etc.).
        6. Optimistic lock loop (up to INV_OPTIMISTIC_LOCK_RETRIES):
           a. WATCH inv:v1:idx:{contact_id}:{coin_category}
           b. current_count = ZCARD on that sorted set
           c. If current_count >= cap: UNWATCH, raise BudgetExceededError
           d. MULTI
              - HSET inv:v1:key:{contact_id}:{key_id} with all fields
              - ZADD inv:v1:idx:{contact_id}:{coin_category} score=fetched_at member=key_id
              EXEC
           e. If EXEC returns None (WATCH failed): retry
           f. If all retries exhausted: raise ConcurrencyError
        7. Return True on success.

        Raises: InvalidCoinCategoryError, ContactNotRegisteredError,
                BudgetExceededError, ConcurrencyError, InventoryUnavailableError

    ─────────────────────────────────────────────
    READ / CONSUME OPERATIONS (HOT PATH)
    ─────────────────────────────────────────────

    select_coin(
        self,
        contact_id:   str,
        desired_tier: str
    ) -> Optional[InventoryEntry]
        The critical hot-path function. Called every time a message is sent.

        1. Validate desired_tier.
        2. Check contact exists (metadata lookup).
        3. Attempt to pop from desired tier:
           a. ZPOPMIN inv:v1:idx:{contact_id}:{desired_tier}
           b. If result: read hash, delete hash, update last_msg_at, return entry.
        4. If desired tier empty, iterate TIER_FALLBACK[desired_tier]:
           a. For each fallback tier: repeat step 3.
        5. If all tiers empty: return None.
           Caller interprets None as "do on-demand fetch from server."

        Performance target: < 2ms total.

        Raises: InvalidCoinCategoryError, ContactNotRegisteredError,
                InventoryUnavailableError

    consume_key(
        self,
        contact_id: str,
        key_id:     str
    ) -> bool
        Explicit key removal (used for error recovery, e.g., signature verification
        failed after select_coin returned the key).

        1. Determine coin_category from the hash entry.
        2. DEL inv:v1:key:{contact_id}:{key_id}
        3. ZREM inv:v1:idx:{contact_id}:{coin_category} key_id
        4. Return True if key existed and was removed, False otherwise.

        Raises: InventoryUnavailableError

    ─────────────────────────────────────────────
    QUERY OPERATIONS
    ─────────────────────────────────────────────

    get_inventory(
        self,
        contact_id: Optional[str] = None
    ) -> dict | InventorySummary
        If contact_id given:
          Returns InventorySummary with ZCARD for each tier + priority from metadata.
        If None:
          SCAN inv:v1:meta:* to discover all contacts.
          For each: build InventorySummary.
          Returns dict[str, InventorySummary].

        Raises: InventoryUnavailableError

    has_keys_for(self, contact_id: str) -> bool
        Quick check: is there at least one key cached for this contact?
        Checks ZCARD on all three tier indexes.
        Returns True if any > 0.
        Used by: message sender to decide between cached path vs on-demand fetch.

        Raises: InventoryUnavailableError

    get_available_tiers(self, contact_id: str) -> list[str]
        Returns list of tiers that have at least one key available.
        E.g., ["SILVER", "BRONZE"] if Gold is empty.
        Used by: Context Manager to know what tiers are possible before
        selecting based on battery/signal.

        Raises: InventoryUnavailableError

    ─────────────────────────────────────────────
    INTERNAL HELPERS (private methods)
    ─────────────────────────────────────────────

    _inv_key(self, contact_id: str, key_id: str) -> str
        Returns f"{INV_KEY_PREFIX}:{contact_id}:{key_id}"

    _idx_key(self, contact_id: str, coin_category: str) -> str
        Returns f"{INV_IDX_PREFIX}:{contact_id}:{coin_category}"

    _meta_key(self, contact_id: str) -> str
        Returns f"{INV_META_PREFIX}:{contact_id}"

    _validate_coin_category(self, coin_category: str) -> None
        Raises InvalidCoinCategoryError if invalid.

    _validate_priority(self, priority: str) -> None
        Raises InvalidPriorityError if invalid.

    _get_priority(self, contact_id: str) -> str
        Reads priority from metadata hash.
        Raises ContactNotRegisteredError if no metadata.

    _pop_from_tier(self, contact_id: str, coin_category: str) -> Optional[InventoryEntry]
        Single-tier pop logic extracted from select_coin.
        ZPOPMIN → HGETALL → DEL hash → return InventoryEntry or None.
        Keeps select_coin clean and the fallback loop readable.

    _trim_excess(self, contact_id: str, new_priority: str) -> int
        Called when contact is downgraded.
        For each tier: compare ZCARD against new BUDGET_CAPS[new_priority][tier].
        If over: ZPOPMAX (remove newest, preserve oldest) N times, DEL corresponding hashes.
        Returns total keys evicted.

    _serialize_entry(self, contact_id, key_id, coin_category, public_key, signature) -> dict
        Builds the flat dict for HSET. Sets fetched_at to current timestamp.

    _deserialize_entry(self, contact_id: str, key_id: str, data: dict) -> InventoryEntry
        Converts HGETALL response to InventoryEntry dataclass.

    _estimate_entry_bytes(self, coin_category: str) -> int
        Returns COIN_SIZE_BYTES[coin_category] + overhead (key name, Redis hash overhead).
        Used by storage calculations.
```

**Imports from project:** `config.*`, `types.InventoryEntry`, `types.ContactMeta`, `types.InventorySummary`, `errors.*`
**Imports external:** `redis`, `time`
**Imported by:** app entrypoint, gc.py, stats.py, tests/test_inventory.py

**Total functions:** 10 public + 8 private = **18 functions**

---

## `aqm_db/gc.py`

**Purpose:** Garbage collector for the Smart Inventory. Runs as a background task when the device is charging. Cleans up stale contacts and their keys.

```
class GarbageCollector:

    __init__(self, inventory: SmartInventory, client: redis.Redis)
        Takes a SmartInventory instance (for metadata access)
        and the raw Redis client (for bulk key deletion).

    ─────────────────────────────────────────────

    garbage_collect(self, inactive_days: int = 30) -> GCResult
        The main entry point. Steps:

        1. SCAN all inv:v1:meta:* keys to get all registered contacts.
        2. For each contact:
           a. Read last_msg_at from metadata.
           b. If now - last_msg_at > inactive_days:
              - Count keys across all three tier indexes (for reporting).
              - Estimate bytes freed using COIN_SIZE_BYTES.
              - Delete all tier indexes: DEL inv:v1:idx:{contact_id}:GOLD, SILVER, BRONZE.
              - SCAN and DEL all inv:v1:key:{contact_id}:* hashes.
              - Update metadata: set priority = "STRANGER".
              - Optionally: DEL the metadata hash entirely (configurable).
              - Increment counters.
        3. Return GCResult(contacts_cleaned, keys_deleted, bytes_freed).

        Raises: InventoryUnavailableError

    collect_single_contact(self, contact_id: str) -> GCResult
        Same logic but for one specific contact. Used for manual cleanup
        (e.g., user blocks someone → immediately purge their keys).

        Raises: ContactNotRegisteredError, InventoryUnavailableError

    dry_run(self, inactive_days: int = 30) -> GCResult
        Identical scan logic but does NOT delete anything.
        Returns what WOULD be cleaned. Useful for UI: "3 contacts
        and 42 keys will be cleaned up."

        Raises: InventoryUnavailableError

    ─────────────────────────────────────────────
    INTERNAL HELPERS
    ─────────────────────────────────────────────

    _delete_all_keys_for_contact(self, contact_id: str) -> int
        Deletes all three sorted set indexes + all key hashes for a contact.
        Uses pipeline for efficiency (batch DEL).
        Returns count of deleted keys.

    _is_inactive(self, last_msg_at: int, inactive_days: int) -> bool
        Compares timestamp against current time. Simple but extracted
        for testability (you can mock time).
```

**Imports from project:** `config.*`, `types.GCResult`, `errors.*`, `inventory.SmartInventory`
**Imports external:** `redis`, `time`
**Imported by:** app scheduler, tests/test_gc.py

**Total functions:** 3 public + 2 private = **5 functions**

---

## `aqm_db/stats.py`

**Purpose:** Storage reporting and inventory analytics. Reads data from both Vault and Inventory to produce reports for the UI and for the pre-fetch scheduler.

```
class StorageReporter:

    __init__(self, vault: SecureVault, inventory: SmartInventory)
        Takes both database instances.

    ─────────────────────────────────────────────

    get_storage_usage(self) -> StorageReport
        1. Get inventory summary for all contacts (inventory.get_inventory()).
        2. For each contact, for each tier: multiply count × COIN_SIZE_BYTES[tier].
        3. Sum totals. Calculate utilization vs INV_MAX_STORAGE_BYTES.
        4. Return StorageReport.

        Raises: InventoryUnavailableError

    get_vault_report(self) -> VaultStats
        Delegates to vault.get_stats().
        Wraps for consistency (all reporting goes through this module).

        Raises: VaultUnavailableError

    get_replenish_needs(self) -> dict[str, dict[str, int]]
        For each registered contact (non-STRANGER):
          Compare current inventory counts against BUDGET_CAPS[priority].
          Calculate deficit per tier.
        Returns: {contact_id: {"GOLD": deficit, "SILVER": deficit, "BRONZE": deficit}}
        Where deficit >= 0 (0 means fully stocked).

        Used by: Pre-fetch scheduler to know exactly how many keys to request
        from the server for each contact.

        Raises: InventoryUnavailableError

    get_full_dashboard(self) -> dict
        Combines everything into one payload for the UI:
        {
            "vault": VaultStats,
            "inventory_storage": StorageReport,
            "replenish_needs": {...},
            "contacts": [InventorySummary, ...],
        }

        Raises: VaultUnavailableError, InventoryUnavailableError
```

**Imports from project:** `config.*`, `types.*`, `vault.SecureVault`, `inventory.SmartInventory`
**Imports external:** None (pure computation on data from other modules)
**Imported by:** app UI layer, pre-fetch scheduler, tests/test_stats.py (if needed)

**Total functions:** 4 public = **4 functions**

---

## `aqm_db/tests/conftest.py`

**Purpose:** Pytest fixtures shared across all test files. Sets up isolated Redis instances (or fakeredis) so tests don't pollute each other.

```
Contains:

fixture vault_client() -> redis.Redis
    Option A: Creates a real Redis client on a test-specific DB (db=15).
    Calls FLUSHDB before and after each test.
    Option B: Uses fakeredis.FakeRedis() for zero-dependency testing.
    Yields the client, tears down after.

fixture inventory_client() -> redis.Redis
    Same as above but on db=14 (or separate FakeRedis instance).

fixture vault(vault_client) -> SecureVault
    Instantiates SecureVault with the test client.

fixture inventory(inventory_client) -> SmartInventory
    Instantiates SmartInventory with the test client.

fixture gc(inventory, inventory_client) -> GarbageCollector
    Instantiates GarbageCollector with test inventory + client.

fixture reporter(vault, inventory) -> StorageReporter
    Instantiates StorageReporter with test vault + inventory.

fixture sample_gold_key() -> dict
    Returns a dict with realistic fake values for a Gold coin entry.
    encrypted_blob = os.urandom(1200), etc.

fixture sample_silver_key() -> dict
    Same for Silver.

fixture sample_bronze_key() -> dict
    Same for Bronze.

fixture registered_bestie(inventory) -> str
    Registers a contact "bob_test_uuid" as BESTIE, returns the contact_id.

fixture registered_mate(inventory) -> str
    Same for MATE.
```

**Imports from project:** `connection`, `vault.SecureVault`, `inventory.SmartInventory`, `gc.GarbageCollector`, `stats.StorageReporter`
**Imports external:** `pytest`, `redis` or `fakeredis`, `os`

---

## `aqm_db/tests/test_vault.py`

**Purpose:** Complete test coverage for SecureVault.

```
Contains test functions:

── Store ──
test_store_key_success(vault, sample_gold_key)
test_store_key_duplicate_raises(vault, sample_gold_key)
test_store_key_invalid_category_raises(vault)
test_store_key_sets_ttl(vault, vault_client, sample_gold_key)
test_store_key_increments_stats(vault, sample_gold_key, sample_silver_key)

── Fetch ──
test_fetch_existing_key(vault, sample_gold_key)
test_fetch_nonexistent_returns_none(vault)
test_fetch_burned_key_returns_none(vault, sample_gold_key)
test_fetch_preserves_binary_blobs(vault, sample_gold_key)

── Burn ──
test_burn_key_success(vault, sample_gold_key)
test_burn_nonexistent_raises(vault)
test_burn_already_burned_raises(vault, sample_gold_key)
test_burn_sets_short_ttl(vault, vault_client, sample_gold_key)
test_burn_decrements_active_count(vault, sample_gold_key)
test_burn_increments_burned_count(vault, sample_gold_key)

── Count ──
test_count_active_all_tiers(vault, sample_gold_key, sample_silver_key)
test_count_active_single_tier(vault, sample_gold_key)
test_count_active_empty_vault(vault)

── Exists ──
test_exists_true(vault, sample_gold_key)
test_exists_false(vault)

── Get All Active IDs ──
test_get_all_active_ids(vault, sample_gold_key, sample_silver_key)
test_get_all_active_ids_filtered(vault, sample_gold_key, sample_silver_key)
test_get_all_active_ids_excludes_burned(vault, sample_gold_key)

── Purge ──
test_purge_expired_removes_old_keys(vault)
    Manually sets created_at to 31 days ago, runs purge, asserts deleted.
test_purge_expired_keeps_recent_keys(vault, sample_gold_key)
test_purge_updates_stats(vault)

── Stats ──
test_get_stats_empty(vault)
test_get_stats_after_operations(vault, sample_gold_key)
```

**Total tests: ~25**

---

## `aqm_db/tests/test_inventory.py`

**Purpose:** Complete test coverage for SmartInventory.

```
Contains test functions:

── Contact Management ──
test_register_contact_success(inventory)
test_register_contact_duplicate_is_idempotent(inventory)
test_register_contact_invalid_priority_raises(inventory)
test_get_contact_meta(inventory, registered_bestie)
test_get_contact_meta_nonexistent_returns_none(inventory)
test_set_priority_upgrade(inventory, registered_mate)
test_set_priority_downgrade_trims_excess(inventory, registered_bestie)
test_set_priority_unregistered_raises(inventory)

── Store ──
test_store_key_success(inventory, registered_bestie, sample_gold_key)
test_store_key_budget_exceeded_raises(inventory, registered_bestie)
    Store 5 Gold keys, attempt 6th → BudgetExceededError.
test_store_key_mate_cannot_store_gold(inventory, registered_mate)
test_store_key_stranger_cannot_store_anything(inventory)
test_store_key_unregistered_contact_raises(inventory)
test_store_key_updates_sorted_set_index(inventory, inventory_client, registered_bestie)

── Select Coin ──
test_select_coin_returns_correct_tier(inventory, registered_bestie)
test_select_coin_fifo_order(inventory, registered_bestie)
    Store 3 Silver keys with known timestamps, select → assert oldest returned first.
test_select_coin_fallback_gold_to_silver(inventory, registered_bestie)
test_select_coin_fallback_silver_to_bronze(inventory, registered_bestie)
test_select_coin_no_upward_fallback(inventory, registered_bestie)
    Request Bronze, only Gold available → returns None (never wastes Gold).
test_select_coin_empty_returns_none(inventory, registered_bestie)
test_select_coin_removes_from_inventory(inventory, registered_bestie)
test_select_coin_updates_last_msg_at(inventory, inventory_client, registered_bestie)

── Consume ──
test_consume_key_success(inventory, registered_bestie)
test_consume_nonexistent_returns_false(inventory, registered_bestie)

── Query ──
test_get_inventory_single_contact(inventory, registered_bestie)
test_get_inventory_all_contacts(inventory, registered_bestie, registered_mate)
test_has_keys_for_true(inventory, registered_bestie)
test_has_keys_for_false(inventory, registered_bestie)
test_get_available_tiers(inventory, registered_bestie)

── Edge Cases ──
test_store_after_select_refills(inventory, registered_bestie)
    Select all keys, verify empty, store new ones, verify available again.
test_priority_downgrade_then_select(inventory, registered_bestie)
    Fill Bestie with Gold keys, downgrade to Mate (Gold cap=0), select → no Gold returned.
```

**Total tests: ~28**

---

## `aqm_db/tests/test_gc.py`

**Purpose:** Test garbage collection logic.

```
Contains test functions:

test_gc_removes_inactive_contacts(gc, inventory, registered_bestie)
    Manually set last_msg_at to 31 days ago, run gc, verify keys deleted.
test_gc_keeps_active_contacts(gc, inventory, registered_bestie)
test_gc_returns_correct_counts(gc, inventory)
test_gc_downgrades_to_stranger(gc, inventory, registered_bestie)
test_collect_single_contact(gc, inventory, registered_bestie)
test_dry_run_does_not_delete(gc, inventory, registered_bestie)
test_gc_handles_empty_inventory(gc)
```

**Total tests: ~7**

---

## `aqm_db/tests/test_concurrency.py`

**Purpose:** Verify no race conditions under concurrent access.

```
Contains test functions:

test_concurrent_store_respects_budget(inventory, registered_bestie)
    Launch 10 async tasks all trying to store Gold keys for the same Bestie.
    Assert exactly 5 succeed, 5 raise BudgetExceededError.

test_concurrent_select_no_duplicates(inventory, registered_bestie)
    Pre-load 5 Silver keys. Launch 10 async tasks all calling select_coin.
    Assert exactly 5 get a key, 5 get None. No key returned twice.

test_concurrent_burn_idempotent(vault)
    Store 1 key. Launch 5 async tasks all calling burn_key.
    Assert exactly 1 succeeds, 4 raise KeyAlreadyBurnedError.

test_concurrent_store_and_select(inventory, registered_bestie)
    One task continuously stores keys, another continuously selects.
    Run for 1 second. Assert no crashes, no duplicates, counts consistent.
```

**Total tests: ~4** (but each is a stress test)

---

## Summary: Function Count by File

| File | Public | Private | Total |
|------|--------|---------|-------|
| `config.py` | — (constants only) | — | — |
| `types.py` | 9 dataclasses | — | 9 |
| `errors.py` | 10 exception classes | — | 10 |
| `connection.py` | 4 | 0 | 4 |
| `vault.py` | 8 | 3 | 11 |
| `inventory.py` | 10 | 8 | 18 |
| `gc.py` | 3 | 2 | 5 |
| `stats.py` | 4 | 0 | 4 |
| **Total** | **39** | **13** | **52 + 19 types/errors** |
| | | | |
| `conftest.py` | 10 fixtures | — | 10 |
| `test_vault.py` | ~25 tests | — | 25 |
| `test_inventory.py` | ~28 tests | — | 28 |
| `test_gc.py` | ~7 tests | — | 7 |
| `test_concurrency.py` | ~4 tests | — | 4 |
| **Test Total** | **~74** | — | **74** |
