from dataclasses import dataclass

@dataclass
class VaultEntry:
    key_id:         str
    coin_category:  str
    encrypted_blob: bytes
    encryption_iv:  bytes
    auth_tag:       bytes
    status:         str
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
    contact_id: str
    priority: str
    last_msg_at: int
    display_name: str

@dataclass
class StorageReport:
    total_bytes: int
    per_contact: dict[str, int]
    budget_bytes: int
    utilization_pct: float

@dataclass
class GCResult:
    contacts_cleaned: int
    keys_deleted:     int
    bytes_freed:      int

@dataclass
class VaultStats:
    active_gold:    int
    active_silver:  int
    active_bronze:  int
    total_burned:   int
    total_expired:  int

@dataclass
class InventorySummary:
    contact_id:  str
    gold_count:  int
    silver_count: int
    bronze_count: int
    priority:    str

@dataclass
class HealthStatus:
    vault_connected:     bool
    inventory_connected: bool
    vault_key_count:     int
    inventory_key_count: int
    uptime_seconds:      float

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
