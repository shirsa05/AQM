# Redis Connection

REDIS_HOST              = "localhost"
REDIS_PORT              = 6379
REDIS_VAULT_DB          = 0          # Logical DB for Secure Vault
REDIS_INVENTORY_DB      = 1          # Logical DB for Smart Inventory
REDIS_SOCKET_TIMEOUT    = 5          # seconds
REDIS_RETRY_ATTEMPTS    = 3
REDIS_RETRY_DELAY       = 0.5       # seconds between retries

# Key Namespace Prefixes

VAULT_KEY_PREFIX        = "vault:v1:key"        # vault:v1:key:{key_id}
VAULT_STATS_KEY         = "vault:v1:stats"

INV_KEY_PREFIX          = "inv:v1:key"           # inv:v1:key:{contact_id}:{key_id}
INV_IDX_PREFIX          = "inv:v1:idx"           # inv:v1:idx:{contact_id}:{coin_category}
INV_META_PREFIX         = "inv:v1:meta"          # inv:v1:meta:{contact_id}

# Vault Settings

VAULT_KEY_TTL_SECONDS       = 2_592_000     # 30 days
VAULT_BURN_GRACE_SECONDS    = 60            # keep burned key for 60s before hard delete
VAULT_PURGE_MAX_AGE_DAYS    = 30

# Inventory Budget Caps (from AQM paper Table 2)

BUDGET_CAPS = {
    "BESTIE":   {"GOLD": 5, "SILVER": 4, "BRONZE": 1},
    "MATE":     {"GOLD": 0, "SILVER": 6, "BRONZE": 4},
    "STRANGER": {"GOLD": 0, "SILVER": 0, "BRONZE": 5},
}

# Inventory Settings

INV_GC_INACTIVE_DAYS        = 30
INV_MAX_STORAGE_BYTES       = 65_536        # 64 KB total budget for all cached keys
INV_OPTIMISTIC_LOCK_RETRIES = 3

# Coin Tier Fallback Order

TIER_FALLBACK = {
    "GOLD":   ["SILVER", "BRONZE"],
    "SILVER": ["BRONZE"],
    "BRONZE": [],
}

# Per-priority tier ceiling — applied after context decision tree
TIER_CEILING = {
    "BESTIE":   "GOLD",     # full range
    "MATE":     "SILVER",   # max SILVER even if context says GOLD
    "STRANGER": "BRONZE",   # always BRONZE regardless of context
}

# Numeric rank for tier comparison (used by ceiling logic)
TIER_RANK = {"GOLD": 3, "SILVER": 2, "BRONZE": 1}

# Valid Enums (for validation)

VALID_COIN_CATEGORIES   = {"GOLD", "SILVER", "BRONZE"}
VALID_PRIORITIES        = {"BESTIE", "MATE", "STRANGER"}
VALID_STATUSES          = {"ACTIVE", "BURNED"}

# Approximate Blob Sizes (bytes, for storage calc)

COIN_SIZE_BYTES = {
    "GOLD":   3_604,    # Kyber-768 pk (1184) + Dilithium sig (2420)
    "SILVER": 1_248,    # Kyber-768 pk (1184) + Ed25519 sig (64)
    "BRONZE": 96,       # X25519 pk (32) + Ed25519 sig (64)
}

CONTACT_THRESHOLDS = {
    "BESTIE_THRESHOLD_7D"    : 5,
    "MATE_THRESHOLD_30D"     : 4,
    "MSG_LOG_RETENTION_DAYS": 30,
}

MESSAGE_TYPE = {"AUTH" , "PARCEL" , "ACK" , "ERROR"}