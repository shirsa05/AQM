"""
AQM Flask UI — Main application server.

Bridges the Flask web interface to the AQM backend subsystems.
Run from project root:
    python -m AQM_Database.flask_app.app --user alice
    python -m AQM_Database.flask_app.app --user bob --port 5001
"""

import random
import json
import os
import queue
import sys
import threading
import time
import uuid
import argparse
import logging
import base64
import hashlib
import hmac
from functools import wraps
from pathlib import Path
import uuid as _uuid_mod

from flask import Flask, Response, jsonify, render_template, request, stream_with_context, redirect, url_for, session


# ── Path setup ──────────────────────────────────────────────────────────────
# AQM_Database/flask_app/app.py → project root is two levels up from flask_app
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from AQM_Database.aqm_db.connection import create_vault_client, create_inventory_client
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
from AQM_Database.aqm_db.stats import StorageReporter
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase
from AQM_Database.aqm_session.ratchet import SessionRatchet
from AQM_Database.aqm_session.session_store import SessionStore
from AQM_Database.aqm_shared.crypto_engine import CryptoEngine
from AQM_Database.aqm_shared.context_manager import ContextManager, DeviceContext
from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_shared import config as aqm_config
from AQM_Database.aqm_shared.config import TIER_CEILING, TIER_RANK
from AQM_Database.bridge import upload_coins, sync_inventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.db import create_pool
from AQM_Database.flask_app.aqm_bridge import run_async
from uuid import UUID


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aqm.flask")

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--user",    default="alice", help="User identity")
parser.add_argument("--port",    type=int, default=5000)
parser.add_argument("--host",    default="127.0.0.1", help="Bind address (0.0.0.0 for Docker)")
parser.add_argument("--contacts", nargs="+", default=None,
                    help="Contact usernames e.g. --contacts bob charlie")
parser.add_argument("--contact-ports", nargs="+", type=int, default=None,
                    help="Ports for each contact in same order e.g. --contact-ports 5001 5002")
parser.add_argument("--password", default=None,
                    help="Login password (or set AQM_PASSWORD env var)")
args, _ = parser.parse_known_args()

USER_ID  = args.user.lower()
PORT     = args.port
BIND_HOST = args.host

# Build contacts list and port map
_default_contacts = ["bob"] if USER_ID == "alice" else ["alice"]
KNOWN_CONTACTS = [c.lower() for c in (args.contacts or _default_contacts)]

_default_ports = {c: (5001 + i) for i, c in enumerate(KNOWN_CONTACTS)}
if args.contact_ports:
    _port_map = dict(zip(KNOWN_CONTACTS, args.contact_ports))
else:
    _port_map = _default_ports
CONTACT_PORTS: dict[str, int] = _port_map

# Keep PARTNER_ID/PARTNER_PORT for backwards compat (first contact)
PARTNER_ID   = KNOWN_CONTACTS[0]
PARTNER_PORT = CONTACT_PORTS[PARTNER_ID]

_DEFAULT_TIER     = "BRONZE"
_DEFAULT_PRIORITY = "STRANGER"


# ── Authentication ────────────────────────────────────────────────────────────
# Password from CLI arg, env var, or default for dev
_RAW_PASSWORD = args.password or os.environ.get("AQM_PASSWORD", "aqm-demo-2026")
# Store as SHA-256 hash so plaintext is never held in memory
_PASSWORD_HASH = hashlib.sha256(_RAW_PASSWORD.encode()).digest()
del _RAW_PASSWORD  # scrub plaintext

def _check_password(password: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(password.encode()).digest(),
        _PASSWORD_HASH,
    )

def login_required(f):
    """Decorator: redirect to login if not authenticated.
    Returns JSON 401 for API/SSE requests, HTML redirect for pages."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/") or request.path == "/stream":
                return jsonify({"error": "not authenticated"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__,
            template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["SESSION_COOKIE_NAME"] = f"aqm_session_{USER_ID}"  # per-user cookie to avoid collision on localhost
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 86400  # 24 hours

# ── AQM subsystem init ────────────────────────────────────────────────────────
vault_client     = create_vault_client()
inv_client       = create_inventory_client()
vault = SecureVault(vault_client, user_id=USER_ID)
inventory        = SmartInventory(inv_client)
reporter         = StorageReporter(vault, inventory)
# --- REAL COIN SERVER CONNECTION ---
server_pool = run_async(create_pool(
    srv_config.PG_DSN,
    srv_config.PG_POOL_MIN_SIZE,
    srv_config.PG_POOL_MAX_SIZE
))
coin_server = CoinInventoryServer(server_pool)

def _make_uuid(username: str) -> UUID:
    """Deterministic UUID from username — same across restarts."""
    return UUID(str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_DNS, f"aqm.{username}")))

# Build UUID map for self + all known contacts
USER_UUIDS: dict[str, UUID] = {
    name: _make_uuid(name)
    for name in [USER_ID] + KNOWN_CONTACTS
}

contacts_db      = ContactsDatabase(db_path=f"~/.aqm/{USER_ID}_contacts.db")
session_store = SessionStore(db_path=str(Path.home() / ".aqm" / f"{USER_ID}_sessions.db"))
crypto           = CryptoEngine()
_crypto_lock     = threading.Lock()   # liboqs is not thread-safe — serialize all crypto calls
context_mgr      = ContextManager()

# In-memory active ratchets
active_ratchets: dict[str, SessionRatchet] = {}

# SSE message queue — incoming messages pushed here for the browser
sse_queue: queue.Queue = queue.Queue(maxsize=100)

# Message history (in-memory, per session)
message_history: list[dict] = []

# Vault stats are read directly from Redis via vault.get_stats() — no in-memory cache

# ── Background minting state ─────────────────────────────────────────────────

# Cooldown: don't mint more often than this (seconds)
_MINT_COOLDOWN_SECS = 120

# Low-watermark: mint if any tier falls below this fraction of its cap
_MINT_LOW_WATERMARK = 0.4   # mint when vault drops to < 40% of target

_mint_lock       = threading.Lock()          # only one mint job at a time
_is_minting      = False                     # currently minting?
_last_mint_time  = 0.0                       # epoch of last successful mint
_last_mint_result: dict = {}                 # {tier: count} from last mint
_mint_ideal_streak = 0                       # consecutive ideal-state ticks


def _vault_is_low() -> bool:
    """Return True if any tier has fallen below the low-watermark threshold."""
    stats = vault.get_stats()
    current = {"GOLD": stats.active_gold, "SILVER": stats.active_silver, "BRONZE": stats.active_bronze}
    targets = {"GOLD": 5, "SILVER": 6, "BRONZE": 5}
    for tier, target in targets.items():
        if target > 0 and current[tier] / target < _MINT_LOW_WATERMARK:
            return True
    return False


def _do_background_mint() -> dict:
    """
    Mint new coins when device is in ideal state and vault is running low.

    Returns dict {tier: minted_count} or raises on failure.
    Runs in the context-simulator thread — must not block for long.
    """
    global _is_minting, _last_mint_time, _last_mint_result

    targets = {
        "GOLD":   5,
        "SILVER": 6,
        "BRONZE": 5,
    }

    minted_counts = {"GOLD": 0, "SILVER": 0, "BRONZE": 0}
    minted_bundles = []

    stats = vault.get_stats()
    current_counts = {"GOLD": stats.active_gold, "SILVER": stats.active_silver, "BRONZE": stats.active_bronze}
    for tier, target in targets.items():
        current = current_counts.get(tier, 0)
        needed  = max(0, target - current)
        if needed == 0:
            continue

        for _ in range(needed):
            with _crypto_lock:
                bundle = crypto.mint_coin(tier)
            dummy_iv       = bytes(12)
            dummy_auth_tag = bytes(16)
            vault.store_key(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                encrypted_blob=bundle.secret_key,
                encryption_iv=dummy_iv,
                auth_tag=dummy_auth_tag,
            )
            minted_bundles.append(bundle)
            minted_counts[tier] += 1

    total = sum(minted_counts.values())
    if total == 0:
        return minted_counts   # vault already full

    logger.info("Background mint: %d new coins %s", total, minted_counts)

    # Upload public keys to server
    uploads = [
        CoinUpload(
            key_id=b.key_id,
            coin_category=b.coin_category,
            public_key_blob=b.public_key,
            signature_blob=b.signature,
        )
        for b in minted_bundles
    ]
    run_async(upload_coins(coin_server, USER_UUIDS[USER_ID], uploads))
    logger.info("Uploaded %d background-minted coins", total)

    # Re-sync inventory for all contacts so they can fetch new coins
    for cid in KNOWN_CONTACTS:
        try:
            fetched = run_async(sync_inventory(
                coin_server, inventory, cid,
                USER_UUIDS[cid], USER_UUIDS[USER_ID],
            ))
            if any(v > 0 for v in fetched.values()):
                logger.info("Post-mint sync for %s: %s", cid, fetched)
        except Exception as e:
            logger.warning("Post-mint sync failed for %s: %s", cid, e)

    _last_mint_time   = time.time()
    _last_mint_result = minted_counts
    return minted_counts


# ── Simulated device context (randomised per message like the CLI demo) ──────

def random_device_context() -> DeviceContext:
    battery = random.uniform(1, 100)        # allow < 5% for critical battery case
    wifi    = random.choice([True, True, False])
    signal  = random.uniform(-120, -60)     # allow < -100 dBm for weak cellular case
    return DeviceContext(battery_pct=battery, wifi_connected=wifi, signal_dbm=signal)

# Module level — current device context (updates independently of sends)
_current_ctx: DeviceContext = random_device_context()
_current_ideal_tier: str = context_mgr.select_coin(_current_ctx)
_current_tier: str = _current_ideal_tier  # after ceiling applied


def _context_simulator():
    """Simulate device context changing independently of message sends.
    Also triggers background minting when device is in ideal state."""
    global _current_ctx, _current_ideal_tier, _current_tier
    global _is_minting, _mint_ideal_streak

    while True:
        time.sleep(20)
        _current_ctx        = random_device_context()
        _current_ideal_tier = context_mgr.select_coin(_current_ctx)

        # Apply ceiling based on first contact's priority for display
        try:
            contact  = contacts_db.get_contact(PARTNER_ID)
            priority = contact.priority if contact else _DEFAULT_PRIORITY
            ceiling  = TIER_CEILING.get(priority, _DEFAULT_TIER)
            _current_tier = _current_ideal_tier if TIER_RANK.get(_current_ideal_tier, 0) <= TIER_RANK.get(ceiling, 0) else ceiling
        except Exception:
            _current_tier = _current_ideal_tier

        # ── Background minting trigger ───────────────────────────
        is_ideal = context_mgr.is_ideal_state(_current_ctx)

        if is_ideal:
            _mint_ideal_streak += 1
        else:
            _mint_ideal_streak = 0

        mint_triggered = False
        mint_result    = {}

        # Trigger if: ideal state, vault low, not already minting, cooldown elapsed
        cooldown_elapsed = (time.time() - _last_mint_time) > _MINT_COOLDOWN_SECS
        should_mint = (
            is_ideal
            and _vault_is_low()
            and not _is_minting
            and cooldown_elapsed
        )

        if should_mint and _mint_lock.acquire(blocking=False):
            try:
                _is_minting = True
                # Signal UI that minting has started
                try:
                    sse_queue.put_nowait({
                        "type": "mint_update",
                        "status": "minting",
                        "vault": vault_stats_dict(),
                    })
                except queue.Full:
                    pass

                mint_result    = _do_background_mint()
                mint_triggered = True
                logger.info("Background mint complete: %s", mint_result)

                try:
                    sse_queue.put_nowait({
                        "type": "mint_update",
                        "status": "complete",
                        "minted": mint_result,
                        "vault":  vault_stats_dict(),
                        "ts":     time.time(),
                    })
                except queue.Full:
                    pass

            except Exception as e:
                logger.warning("Background mint failed: %s", e)
                try:
                    sse_queue.put_nowait({
                        "type":   "mint_update",
                        "status": "failed",
                        "error":  str(e),
                    })
                except queue.Full:
                    pass
            finally:
                _is_minting = False
                _mint_lock.release()

        # Push context update (includes mint/ideal state info)
        try:
            sse_queue.put_nowait({
                "type": "context_update",
                "ctx": {
                    "battery":      round(_current_ctx.battery_pct, 1),
                    "wifi":         _current_ctx.wifi_connected,
                    "signal":       round(_current_ctx.signal_dbm, 1),
                    "ideal_tier":   _current_ideal_tier,
                    "tier":         _current_tier,
                    "is_ideal":     is_ideal,
                    "is_minting":   _is_minting,
                    "ideal_streak": _mint_ideal_streak,
                },
            })
        except queue.Full:
            pass


_context_thread = threading.Thread(target=_context_simulator, daemon=True)
_context_thread.start()

# ── Bootstrap: mint coins + register partner contact ─────────────────────────

_last_known_priority: dict[str, str] = {}

def _update_inventory_priority(contact_id: str, new_priority: str):
    """Write or update the full inventory meta hash from SQLite source of truth."""
    meta_key = f"{aqm_config.INV_META_PREFIX}:{contact_id}"
    try:
        inv_client.hset(meta_key, mapping={
            "contact_id":   contact_id,
            "priority":     new_priority,
            "display_name": contact_id.capitalize(),
            "last_msg_at":  str(int(time.time() * 1000)),
        })
        logger.info("Updated inventory priority for %s -> %s", contact_id, new_priority)
    except Exception as e:
        logger.warning("Could not update inventory priority: %s", e)

def bootstrap():
    """Mint coins for self, register all known contacts."""
    logger.info("Bootstrapping AQM for user: %s", USER_ID)

    targets = {"GOLD": 5, "SILVER": 6, "BRONZE": 5}
    minted, minted_bundles = 0, []

    for tier, count in targets.items():
        for _ in range(count):
            with _crypto_lock:
                bundle = crypto.mint_coin(tier)
            minted_bundles.append(bundle)
            dummy_iv       = bytes(12)
            dummy_auth_tag = bytes(16)
            vault.store_key(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                encrypted_blob=bundle.secret_key,
                encryption_iv=dummy_iv,
                auth_tag=dummy_auth_tag,
            )
            minted += 1
    logger.info("Minted %d new coins", minted)

    uploads = [
        CoinUpload(
            key_id=b.key_id,
            coin_category=b.coin_category,
            public_key_blob=b.public_key,
            signature_blob=b.signature,
        )
        for b in minted_bundles
    ]
    if uploads:
        logger.info("Uploading %d coins to server", len(uploads))
        run_async(upload_coins(coin_server, USER_UUIDS[USER_ID], uploads))
        logger.info("Uploaded %d coins", len(uploads))

    # Register every known contact
    for cid in KNOWN_CONTACTS:
        try:
            existing = contacts_db.get_contact(cid)
            if not existing:
                contacts_db.add_contact(cid, cid.capitalize())
                existing = contacts_db.get_contact(cid)

            correct_priority = existing.priority if existing else "STRANGER"
            _last_known_priority[cid] = correct_priority

            # Force-set Redis meta from SQLite source of truth
            _update_inventory_priority(cid, correct_priority)
            # register_contact only inserts if Redis key is missing
            inventory.register_contact(cid, correct_priority, cid.capitalize())

            logger.info("Contact %s restored with priority: %s", cid, correct_priority)
        except Exception as e:
            logger.warning("Could not register contact %s: %s", cid, e)

    # Sync inventory from server for each contact
    for cid in KNOWN_CONTACTS:
        try:
            fetched = run_async(sync_inventory(
                coin_server,
                inventory,
                cid,
                USER_UUIDS[cid],
                USER_UUIDS[USER_ID],
            ))
            logger.info("Inventory synced for %s: %s", cid, fetched)
        except Exception as e:
            logger.warning("Server sync failed for %s — no coins available until partner is online: %s", cid, e)


def _on_priority_change(contact_id: str, new_priority: str):
    """Re-sync inventory and update Redis meta when priority changes."""
    old = _last_known_priority.get(contact_id)
    if old == new_priority:
        return  # no change

    _last_known_priority[contact_id] = new_priority
    if old is not None:
        logger.info("Priority change: %s %s -> %s", contact_id, old, new_priority)

    # Update inventory Redis meta so sync_inventory uses new caps
    try:
        _update_inventory_priority(contact_id, new_priority)
    except Exception:
        pass

    # Re-sync from server with new (higher) caps
    if contact_id in USER_UUIDS:
        try:
            fetched = run_async(sync_inventory(
                coin_server, inventory,
                contact_id,
                USER_UUIDS[contact_id],
                USER_UUIDS[USER_ID],
            ))
            if any(v > 0 for v in fetched.values()):
                logger.info("Post-promotion sync for %s: %s", contact_id, fetched)
                sse_queue.put_nowait({"type": "status_update"})
        except Exception as e:
            logger.warning("Post-promotion sync failed: %s", e)

bootstrap()

def _background_sync():
    """Retry inventory sync for any contact still showing 0 coins."""
    while True:
        time.sleep(10)
        for cid in KNOWN_CONTACTS:
            try:
                summary = inventory.get_inventory(cid)
                total = summary.gold_count + summary.silver_count + summary.bronze_count
                if total == 0:
                    fetched = run_async(sync_inventory(
                        coin_server, inventory,
                        cid, USER_UUIDS[cid], USER_UUIDS[USER_ID],
                    ))
                    if any(v > 0 for v in fetched.values()):
                        logger.info("Inventory replenished for %s: G=%d S=%d B=%d",
                                    cid,
                                    fetched.get("GOLD", 0),
                                    fetched.get("SILVER", 0),
                                    fetched.get("BRONZE", 0))
                        try:
                            sse_queue.put_nowait({"type": "status_update"})
                        except queue.Full:
                            pass
                    else:
                        logger.debug("Background sync — no new coins for %s (partner offline?)", cid)
            except Exception as e:
                logger.debug("Background sync failed for %s: %s", cid, e)

_sync_thread = threading.Thread(target=_background_sync, daemon=True)
_sync_thread.start()


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_ratchet(contact_id: str) -> SessionRatchet | None:
    if contact_id in active_ratchets:
        return active_ratchets[contact_id]
    r = session_store.load_ratchet(contact_id)
    if r:
        active_ratchets[contact_id] = r
    return r


def save_ratchet(r: SessionRatchet):
    active_ratchets[r.contact_id] = r
    try:
        session_store.save_ratchet(r)
        logger.info("DEBUG: saved ratchet contact=%s send_counter=%d has_sent_first=%s", 
                    r.contact_id, r.send_counter, r.has_sent_first)
    except Exception as e:
        logger.error("SAVE RATCHET FAILED: %s", e, exc_info=True)


def tier_color(tier: str) -> str:
    return {"GOLD": "#FFD700", "SILVER": "#C0C0C0", "BRONZE": "#CD7F32"}.get(tier, "#888")


def coin_counts(contact_id: str = None) -> dict:
    """Return inventory for a specific contact, or summed across all."""
    try:
        targets = [contact_id] if contact_id else KNOWN_CONTACTS
        gold = silver = bronze = 0
        for cid in targets:
            s = inventory.get_inventory(cid)
            gold   += s.gold_count
            silver += s.silver_count
            bronze += s.bronze_count
        return {"gold": gold, "silver": silver, "bronze": bronze}
    except Exception:
        return {"gold": 0, "silver": 0, "bronze": 0}


def vault_stats_dict() -> dict:
    stats = vault.get_stats()
    return {
        "active_gold":   stats.active_gold,
        "active_silver": stats.active_silver,
        "active_bronze": stats.active_bronze,
        "total_burned":  stats.total_burned,
        "total_expired": stats.total_expired,
    }


def mint_status_dict() -> dict:
    """Current background minting state for API/SSE."""
    cooldown_remaining = max(0.0, _MINT_COOLDOWN_SECS - (time.time() - _last_mint_time))
    return {
        "is_minting":          _is_minting,
        "is_ideal":            context_mgr.is_ideal_state(_current_ctx),
        "vault_is_low":        _vault_is_low(),
        "last_mint_ts":        _last_mint_time if _last_mint_time > 0 else None,
        "last_mint_result":    _last_mint_result,
        "cooldown_remaining":  round(cooldown_remaining),
        "cooldown_total":      _MINT_COOLDOWN_SECS,
        "ideal_streak":        _mint_ideal_streak,
    }


def contacts_list() -> list[dict]:
    try:
        all_contacts = contacts_db.get_all_contacts() or []
        result = []
        for c in all_contacts:
            inv_summary = None
            try:
                inv_summary = inventory.get_inventory(c.contact_id)
            except Exception:
                pass
            result.append({
                "contact_id":     c.contact_id,
                "display_name":   c.display_name,
                "priority":       c.priority,
                "priority_locked": bool(c.priority_locked),
                "msg_count_total": c.msg_count_total,
                "msg_count_7d":   c.msg_count_7d,
                "msg_count_30d":  c.msg_count_30d,
                "is_blocked":     bool(c.is_blocked),
                "last_msg_at":    str(c.last_msg_at) if c.last_msg_at else None,
                "coins": {
                    "gold":   inv_summary.gold_count   if inv_summary else 0,
                    "silver": inv_summary.silver_count if inv_summary else 0,
                    "bronze": inv_summary.bronze_count if inv_summary else 0,
                } if inv_summary else {"gold": 0, "silver": 0, "bronze": 0},
            })
        return result
    except Exception as e:
        logger.warning("contacts_list error: %s", e)
        return []


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if _check_password(password):
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("index"))
        return render_template("login.html", user_id=USER_ID, error="Invalid password")
    return render_template("login.html", user_id=USER_ID, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html",
                           user_id=USER_ID,
                           partner_id=PARTNER_ID,
                           partner_port=PARTNER_PORT,
                           known_contacts=KNOWN_CONTACTS)


@app.route("/api/status")
@login_required
def api_status():
    counts = coin_counts()
    vstats = vault_stats_dict()
    return jsonify({
        "user_id":    USER_ID,
        "partner_id": PARTNER_ID,
        "coins":      counts,
        "vault":      vstats,
        "contacts":   contacts_list(),
        "context": {
            "battery":    round(_current_ctx.battery_pct, 1),
            "wifi":       _current_ctx.wifi_connected,
            "signal":     round(_current_ctx.signal_dbm, 1),
            "ideal_tier": _current_ideal_tier,
            "tier":       _current_tier,
            "is_ideal":   context_mgr.is_ideal_state(_current_ctx),
            "is_minting": _is_minting,
        },
        "minting": mint_status_dict(),
    })


@app.route("/api/mint/status")
@login_required
def api_mint_status():
    return jsonify(mint_status_dict())


@app.route("/api/send", methods=["POST"])
@login_required
def api_send():
    data       = request.get_json(force=True) or {}
    plaintext  = data.get("message", "").strip()
    contact_id = data.get("contact_id", PARTNER_ID)

    if not plaintext:
        return jsonify({"error": "empty message"}), 400
    if contact_id not in KNOWN_CONTACTS:
        return jsonify({"error": f"unknown contact: {contact_id}"}), 400

    ctx = _current_ctx

    # Get or create ratchet FIRST — only consume a coin if rekey needed
    ratchet = get_ratchet(contact_id)
    kem_ct, coin_id_used, coin = None, None, None

    if ratchet is not None and not ratchet.needs_rekey():
        # ── Active session — context/tier changes are IRRELEVANT ──────────
        # Tier is locked to whatever was negotiated at session start.
        # The ratchet continues until send_counter hits max_messages.
        coin = type('Coin', (), {
            'coin_category': ratchet.coin_tier,
            'key_id': None,
        })()
    else:
        # ── New session or ratchet exhausted — evaluate context NOW ────────
        # This is the ONLY moment context determines the tier.
        # Once the coin is consumed and the ratchet initialised, context
        # changes have no effect until the next rekey.
        contact  = contacts_db.get_contact(contact_id)
        priority = contact.priority if contact else "STRANGER"
        ceiling  = TIER_CEILING.get(priority, "BRONZE")
        ideal_tier = _current_ideal_tier
        tier = ideal_tier if TIER_RANK.get(ideal_tier, 0) <= TIER_RANK.get(ceiling, 0) else ceiling

        coin = inventory.select_coin(contact_id, tier)
        if coin is None:
            return jsonify({"error": "no coins available"}), 503
        logger.info("Coin consumed (rekey) — contact=%s tier=%s key_id=%s remaining=%s",
                    contact_id, tier, coin.key_id, coin_counts(contact_id))
        with _crypto_lock:
            ct, shared_secret = crypto.kem_encapsulate(coin.public_key, coin.coin_category)
        kem_ct = ct
        if ratchet is None:
            ratchet = SessionRatchet(contact_id, coin.coin_category, shared_secret, is_initiator=True)
        else:
            ratchet.rekey(shared_secret, coin.coin_category, is_initiator=True)
        coin_id_used = coin.key_id

    msg_key     = ratchet.derive_send_key()
    aad         = f"{USER_ID}:{contact_id}".encode()
    with _crypto_lock:
        enc_payload = crypto.encrypt_aead(plaintext.encode(), msg_key, aad)
    save_ratchet(ratchet)

    parcel = {
        "sender_id":         USER_ID,
        "recipient_id":      contact_id,
        "encrypted_payload": base64.b64encode(enc_payload).decode(),
        "aad":               base64.b64encode(aad).decode(),
        "coin_tier":         coin.coin_category,
        "plaintext":         plaintext,
        "device_ctx": {
            "battery": round(ctx.battery_pct, 1),
            "wifi":    ctx.wifi_connected,
            "signal":  round(ctx.signal_dbm, 1),
        },
    }
    if coin_id_used:
        parcel["coin_id"]        = coin_id_used
        parcel["kem_ciphertext"] = base64.b64encode(kem_ct).decode()

    msg_record = {
        "id":        str(uuid.uuid4()),
        "sender":    USER_ID,
        "recipient": contact_id,
        "text":      plaintext,
        "tier":      coin.coin_category,
        "tier_color": tier_color(coin.coin_category),
        "device_ctx": parcel["device_ctx"],
        "ts":        time.time(),
        "rekey":     coin_id_used is not None,
        "msg_count": ratchet.send_counter,
        "max_msgs":  ratchet.max_messages,
    }
    message_history.append(msg_record)

    try:
        updated = contacts_db.record_message(contact_id, direction="SENT")
        if updated:
            _on_priority_change(contact_id, updated.priority)
    except Exception:
        pass

    try:
        sse_queue.put_nowait({"type": "message", "data": msg_record})
    except queue.Full:
        pass
    try:
        sse_queue.put_nowait({"type": "status_update"})
    except queue.Full:
        pass

    _forward_to_partner(parcel, msg_record, contact_id)

    return jsonify({"ok": True, "message": msg_record, "coins": coin_counts()})


def _forward_to_partner(parcel: dict, msg_record: dict, contact_id: str):
    """POST the parcel to the contact's Flask instance."""
    import urllib.request, urllib.error
    port = CONTACT_PORTS.get(contact_id, PARTNER_PORT)
    # In Docker mode, use container hostnames (e.g. http://bob:5001)
    # otherwise use localhost
    base_host = os.environ.get("AQM_CONTACT_HOST_TEMPLATE", "localhost")
    if base_host == "docker":
        # Docker service names match contact IDs
        partner_url = f"http://{contact_id}:{port}/api/receive"
    else:
        partner_url = f"http://{base_host}:{port}/api/receive"
    payload = json.dumps({"parcel": parcel, "msg_record": msg_record}).encode()
    req = urllib.request.Request(
        partner_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        logger.debug("Could not forward to %s (offline?): %s", contact_id, e)


@app.route("/api/receive", methods=["POST"])
def api_receive():
    """Called by partner's Flask instance to deliver a message."""
    data       = request.get_json(force=True) or {}
    parcel     = data.get("parcel", {})
    msg_record = data.get("msg_record", {})

    # Decrypt if we have a ratchet (best-effort for demo)
    sender = parcel.get("sender_id", "")

    ratchet = get_ratchet(sender)
    decrypted_text = parcel.get("plaintext", "")  # fallback: use included plaintext

    if "kem_ciphertext" in parcel and "coin_id" in parcel:
        # ── Rekey parcel received — must burn our private key immediately ──
        # Both sides burn: sender consumed the public key (zpopmin in inventory),
        # receiver burns the private key here. After this, the coin is gone on both ends.
        try:
            coin_id   = parcel["coin_id"]
            coin_tier = parcel.get("coin_tier", "BRONZE")
            kem_ct    = base64.b64decode(parcel["kem_ciphertext"])
            entry     = vault.fetch_key(coin_id)

            if entry is None:
                # Key already burned (duplicate delivery) or never existed
                logger.warning("Vault miss on receive — coin_id=%s already burned or unknown", coin_id)
            else:
                with _crypto_lock:
                    shared_secret = crypto.kem_decapsulate(kem_ct, entry.encrypted_blob, coin_tier)

                # ── BURN immediately — one-time key, non-negotiable ──
                # vault.burn_key() updates Redis stats atomically
                vault.burn_key(coin_id)
                stats = vault.get_stats()
                logger.info("Key burned — coin_id=%s tier=%s active=G%d/S%d/B%d burned_total=%d",
                    coin_id, coin_tier,
                    stats.active_gold, stats.active_silver, stats.active_bronze,
                    stats.total_burned)

                if ratchet is None:
                    ratchet = SessionRatchet(sender, coin_tier, shared_secret, is_initiator=False)
                else:
                    ratchet.rekey_recv_only(shared_secret, coin_tier, is_initiator=False)

        except Exception as e:
            logger.warning("KEM decap failed: %s", e)

    if ratchet:
        try:
            msg_key  = ratchet.derive_recv_key()
            aad      = f"{sender}:{USER_ID}".encode()
            enc_data = base64.b64decode(parcel["encrypted_payload"])
            with _crypto_lock:
                decrypted_text = crypto.decrypt_aead(enc_data, msg_key, aad).decode()
        except Exception as e:
            logger.debug("Decrypt failed, using plaintext fallback: %s", e)
        save_ratchet(ratchet)

    # Build incoming message record
    incoming = {
        "id":         str(uuid.uuid4()),
        "sender":     sender,
        "recipient":  USER_ID,
        "text":       decrypted_text,
        "tier":       parcel.get("coin_tier", "BRONZE"),
        "tier_color": tier_color(parcel.get("coin_tier", "BRONZE")),
        "device_ctx": parcel.get("device_ctx", {}),
        "ts":         time.time(),
        "rekey":      "kem_ciphertext" in parcel,
        "msg_count":  ratchet.recv_counter if ratchet else 0,
        "max_msgs":   ratchet.max_messages if ratchet else 0,
        "incoming":   True,
    }
    message_history.append(incoming)

    try:
        updated = contacts_db.record_message(sender, direction="RECEIVED")
        if updated:
            _on_priority_change(sender, updated.priority)
    except Exception:
        pass

    try:
        sse_queue.put_nowait({"type": "message", "data": incoming})
    except queue.Full:
        pass
    try:
        sse_queue.put_nowait({"type": "status_update"})
    except queue.Full:
        pass

    return jsonify({"ok": True})


@app.route("/api/history")
@login_required
def api_history():
    return jsonify({"messages": message_history[-100:]})


@app.route("/api/contacts")
@login_required
def api_contacts():
    return jsonify({"contacts": contacts_list()})


@app.route("/api/contacts/<contact_id>/priority", methods=["POST"])
@login_required
def api_set_priority(contact_id):
    data     = request.get_json(force=True) or {}
    priority = data.get("priority", "STRANGER")
    locked   = data.get("locked", True)
    if locked:
        contacts_db.lock_priority(contact_id, priority)
    else:
        contacts_db.unlock_priority(contact_id)
    return jsonify({"ok": True, "contact": contact_id, "priority": priority})


@app.route("/api/vault")
@login_required
def api_vault():
    return jsonify(vault_stats_dict())


@app.route("/api/inventory")
@login_required
def api_inventory():
    return jsonify({"coins": coin_counts(), "partner": PARTNER_ID})

@app.route("/api/debug/server-coins")
@login_required
def api_debug_server_coins():
    """Debug endpoint: show coins stored on the PostgreSQL coin server."""
    try:
        coins = run_async(
            coin_server.fetch_coins(
                USER_UUIDS[USER_ID],   # owner of coins
                USER_UUIDS[USER_ID],   # requester
                "GOLD",                # tier (temporary)
                100                    # max fetch
            )
        )

        return jsonify({
            "count": len(coins),
            "coins": [
                {
                    "key_id": c.key_id,
                    "tier": c.coin_category
                }
                for c in coins
            ]
        })

    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/stream")
@login_required
def stream():
    """SSE endpoint — browser subscribes here for real-time updates."""
    def event_generator():
        # Send initial state
        yield f"data: {json.dumps({'type': 'connected', 'user': USER_ID})}\n\n"
        while True:
            try:
                item = sse_queue.get(timeout=25)
                if item.get("type") == "status_update":
                    payload = {
                        "type":     "status_update",
                        "coins":    coin_counts(),
                        "vault":    vault_stats_dict(),
                        "contacts": contacts_list(),
                        "minting":  mint_status_dict(),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                # Keepalive ping
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(event_generator()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    logger.info("Starting AQM Flask UI for user=%s on %s:%d", USER_ID, BIND_HOST, PORT)
    logger.info("Partner=%s expected on port=%d", PARTNER_ID, PARTNER_PORT)
    logger.info("Default password: aqm-demo-2026 (override with --password or AQM_PASSWORD)")
    app.run(host=BIND_HOST, port=PORT, debug=False, threaded=True)