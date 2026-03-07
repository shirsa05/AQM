"""
AQM Prototype Demo — End-to-end 4-phase lifecycle.

Requires Docker (Redis 7 + PostgreSQL 16):
    cd AQM_Database && docker-compose up -d

Run:
    python -m AQM_Database.prototype
"""

import asyncio
import uuid

from AQM_Database.aqm_shared.crypto_engine import CryptoEngine, mint_coin
from AQM_Database.aqm_shared.context_manager import (
    ContextManager,
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
)
from AQM_Database.aqm_shared import config
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.db import create_pool, close_pool
from AQM_Database.bridge import upload_coins, fetch_and_cache


# ─── ANSI Display Helpers ───

class Display:
    """Terminal formatting with ANSI colors — zero external dependencies."""

    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    ORANGE  = "\033[38;5;208m"

    TIER_COLORS = {
        "GOLD":   YELLOW,
        "SILVER": WHITE,
        "BRONZE": ORANGE,
    }

    @classmethod
    def phase_header(cls, number: int, title: str) -> None:
        line = "═" * 60
        print(f"\n{cls.CYAN}{cls.BOLD}{line}")
        print(f"  PHASE {number} — {title}")
        print(f"{line}{cls.RESET}\n")

    @classmethod
    def arrow(cls, msg: str) -> None:
        print(f"  {cls.BLUE}→{cls.RESET} {msg}")

    @classmethod
    def success(cls, msg: str) -> None:
        print(f"  {cls.GREEN}✓{cls.RESET} {msg}")

    @classmethod
    def stat_row(cls, label: str, value, width: int = 28) -> None:
        print(f"  {label:<{width}} {cls.BOLD}{value}{cls.RESET}")

    @classmethod
    def tier_label(cls, tier: str) -> str:
        color = cls.TIER_COLORS.get(tier, cls.WHITE)
        return f"{color}{cls.BOLD}{tier}{cls.RESET}"

    @classmethod
    def table(cls, headers: list[str], rows: list[list], col_width: int = 14) -> None:
        header_line = "".join(f"{h:<{col_width}}" for h in headers)
        print(f"\n  {cls.BOLD}{header_line}{cls.RESET}")
        print(f"  {'─' * (col_width * len(headers))}")
        for row in rows:
            cells = []
            for i, cell in enumerate(row):
                s = str(cell)
                # Color tier names
                if s in cls.TIER_COLORS:
                    s = cls.tier_label(s)
                    # Pad accounting for ANSI escape chars
                    cells.append(s + " " * max(0, col_width - len(str(cell))))
                else:
                    cells.append(f"{s:<{col_width}}")
            print(f"  {''.join(cells)}")
        print()

    @classmethod
    def section(cls, title: str) -> None:
        print(f"\n  {cls.MAGENTA}{cls.BOLD}── {title} ──{cls.RESET}")

    @classmethod
    def banner(cls) -> None:
        print(f"""
{cls.CYAN}{cls.BOLD}
    ╔═══════════════════════════════════════════════════╗
    ║     AQM — Amortized Quantum Messaging Demo        ║
    ║     Post-Quantum Key Lifecycle Prototype          ║
    ╚═══════════════════════════════════════════════════╝
{cls.RESET}""")


# ─── Prototype Logic ───

# Identities
BOB_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ALICE_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
BOB_CONTACT_ID = "bob"

# Constant mint plan: 5 GOLD, 6 SILVER, 5 BRONZE
MINT_PLAN = [("GOLD", 5), ("SILVER", 6), ("BRONZE", 5)]


async def phase1_mint(engine: CryptoEngine, vault: SecureVault, server: CoinInventoryServer):
    """MINT: Generate keypairs → store private in vault → upload public to server."""
    Display.phase_header(1, "MINT — Key Generation & Distribution")
    Display.arrow(f"Crypto backend: liboqs (Kyber-768 + Dilithium-3)")

    all_uploads: list[CoinUpload] = []

    for tier, count in MINT_PLAN:
        Display.section(f"Minting {count}× {tier}")
        for i in range(count):
            bundle = mint_coin(engine, tier)

            # Store private key in vault
            vault.store_key(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                encrypted_blob=bundle.encrypted_blob,
                encryption_iv=bundle.encryption_iv,
                auth_tag=bundle.auth_tag,
            )

            # Prepare public key for server upload
            all_uploads.append(CoinUpload(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                public_key_blob=bundle.public_key,
                signature_blob=bundle.signature,
            ))

            Display.success(
                f"[{i+1}/{count}] {Display.tier_label(tier)}  "
                f"id={bundle.key_id[:8]}…  pk={len(bundle.public_key)}B  "
                f"sk→vault={len(bundle.encrypted_blob)}B"
            )

    # Upload all public keys to server
    Display.section("Uploading public keys to server")
    inserted = await upload_coins(server, BOB_USER_ID, all_uploads)
    Display.success(f"Uploaded {inserted} coins to PostgreSQL")

    # Show stats
    Display.section("Post-mint state")
    stats = vault.get_stats()
    srv_inv = await server.get_inventory_count(BOB_USER_ID)

    Display.table(
        ["Store", "Gold", "Silver", "Bronze"],
        [
            ["Vault (priv)", stats.active_gold, stats.active_silver, stats.active_bronze],
            ["Server (pub)", srv_inv.gold, srv_inv.silver, srv_inv.bronze],
        ],
    )

    return all_uploads


async def phase2_prefetch(inventory: SmartInventory, server: CoinInventoryServer):
    """PRE-FETCH: Register Bob as BESTIE → fetch & cache all his public keys."""
    Display.phase_header(2, "PRE-FETCH — Populate Local Inventory")

    # Register contact
    inventory.register_contact(BOB_CONTACT_ID, "BESTIE", display_name="Bob")
    Display.success(f"Registered '{BOB_CONTACT_ID}' as BESTIE")

    caps = config.BUDGET_CAPS["BESTIE"]
    Display.arrow(f"Budget caps: G={caps['GOLD']} S={caps['SILVER']} B={caps['BRONZE']}")

    # Fetch & cache for each tier
    for tier in ("GOLD", "SILVER", "BRONZE"):
        want = caps[tier]
        if want == 0:
            continue
        cached = await fetch_and_cache(
            server, inventory, BOB_CONTACT_ID,
            BOB_USER_ID, ALICE_USER_ID, tier, want,
        )
        Display.success(f"Fetched {len(cached)}× {Display.tier_label(tier)} → local inventory")

    # Show inventory
    Display.section("Local inventory (Alice's cache of Bob's keys)")
    summary = inventory.get_inventory(BOB_CONTACT_ID)
    Display.table(
        ["Tier", "Cached", "Cap"],
        [
            ["GOLD", summary.gold_count, caps["GOLD"]],
            ["SILVER", summary.silver_count, caps["SILVER"]],
            ["BRONZE", summary.bronze_count, caps["BRONZE"]],
        ],
    )

    # Show server is drained
    srv_inv = await server.get_inventory_count(BOB_USER_ID)
    Display.section("Server inventory (post-fetch)")
    Display.stat_row("Remaining on server:", f"G={srv_inv.gold} S={srv_inv.silver} B={srv_inv.bronze}")
    if srv_inv.gold == 0 and srv_inv.silver == 0 and srv_inv.bronze == 0:
        Display.success("Delete-on-Fetch verified — server fully drained")


def phase3_send(inventory: SmartInventory, cm: ContextManager):
    """SEND: Device context → tier selection → consume coin from inventory."""
    Display.phase_header(3, "SEND — Context-Aware Coin Selection")

    scenarios = [
        ("A", SCENARIO_A),
        ("B", SCENARIO_B),
        ("C", SCENARIO_C),
    ]

    selected_keys = []

    for label, ctx in scenarios:
        tier = cm.select_coin(ctx)
        Display.section(f"Scenario {label}: {ctx.label}")
        Display.stat_row("Battery:", f"{ctx.battery_pct}%")
        Display.stat_row("WiFi:", "yes" if ctx.wifi_connected else "no")
        Display.stat_row("Signal:", f"{ctx.signal_dbm} dBm")
        Display.arrow(f"Selected tier: {Display.tier_label(tier)}")

        entry = inventory.select_coin(BOB_CONTACT_ID, tier)
        if entry:
            Display.success(
                f"Consumed coin id={entry.key_id[:8]}…  "
                f"tier={Display.tier_label(entry.coin_category)}  pk={len(entry.public_key)}B"
            )
            selected_keys.append(entry)
        else:
            Display.arrow(f"No coins available for {tier} (or fallback tiers)")

    # Summary
    Display.section("Coins consumed this session")
    Display.table(
        ["Scenario", "Tier", "Key ID"],
        [
            [f"Scenario {chr(65+i)}", k.coin_category, k.key_id[:12] + "…"]
            for i, k in enumerate(selected_keys)
        ],
    )

    return selected_keys


def phase4_decrypt_burn(vault: SecureVault, selected_keys: list):
    """DECRYPT+BURN: Retrieve private key → burn after use → verify gone."""
    Display.phase_header(4, "DECRYPT + BURN — One-Time Key Destruction")

    if not selected_keys:
        Display.arrow("No keys to burn (no coins were consumed in Phase 3)")
        return

    # Use the first consumed coin for the demo
    target = selected_keys[0]
    Display.arrow(f"Target key: {target.key_id[:12]}…  tier={Display.tier_label(target.coin_category)}")

    # Fetch from vault
    entry = vault.fetch_key(target.key_id)
    if entry:
        Display.success(f"Private key retrieved from vault — status={entry.status}")
        Display.stat_row("Encrypted blob:", f"{len(entry.encrypted_blob)} bytes")
    else:
        Display.arrow("Key not found in vault (may have been minted by a different party)")
        return

    # Burn
    vault.burn_key(target.key_id)
    Display.success("Key BURNED — marked for deletion")

    # Verify it's gone
    after = vault.fetch_key(target.key_id)
    if after is None:
        Display.success("Verification: fetch_key() returns None — burn confirmed")
    else:
        Display.arrow(f"WARNING: key still accessible (status={after.status})")

    # Final stats
    Display.section("Final vault state")
    stats = vault.get_stats()
    Display.table(
        ["Metric", "Value"],
        [
            ["Active GOLD", stats.active_gold],
            ["Active SILVER", stats.active_silver],
            ["Active BRONZE", stats.active_bronze],
            ["Total burned", stats.total_burned],
            ["Total expired", stats.total_expired],
        ],
        col_width=20,
    )


async def main():
    Display.banner()

    # ─── Connect to infrastructure ───
    Display.arrow("Connecting to Redis (vault db=0, inventory db=1)…")
    vault_client = create_vault_client()
    inv_client = create_inventory_client()

    Display.arrow(f"Connecting to PostgreSQL ({srv_config.PG_DSN})…")
    pool = await create_pool(srv_config.PG_DSN, srv_config.PG_POOL_MIN_SIZE, srv_config.PG_POOL_MAX_SIZE)

    vault = SecureVault(vault_client)
    inventory = SmartInventory(inv_client)
    server = CoinInventoryServer(pool)

    # Clean slate for demo
    Display.arrow("Flushing demo databases…")
    vault_client.flushdb()
    inv_client.flushdb()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM coin_inventory WHERE user_id = $1", BOB_USER_ID)

    Display.success("Infrastructure ready\n")

    # ─── Initialize engines ───
    engine = CryptoEngine()
    cm = ContextManager()

    try:
        # Phase 1: Mint
        await phase1_mint(engine, vault, server)

        # Phase 2: Pre-fetch
        await phase2_prefetch(inventory, server)

        # Phase 3: Send
        selected = phase3_send(inventory, cm)

        # Phase 4: Decrypt + Burn
        phase4_decrypt_burn(vault, selected)

        # Finish
        print(f"\n{Display.GREEN}{Display.BOLD}  ══ Demo complete ══{Display.RESET}\n")

    finally:
        vault_client.close()
        inv_client.close()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
