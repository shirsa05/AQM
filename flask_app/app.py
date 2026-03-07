"""
AQM Flask UI — Main application server.

Bridges the Flask web interface to the AQM backend subsystems.
Run with:
    python flask_app/app.py --user alice
    python flask_app/app.py --user bob --port 5001
"""

import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
import argparse
import logging
from pathlib import Path
import base64
import uuid as _uuid_mod

from flask import Flask, Response, jsonify, render_template, request, stream_with_context


# ── Path setup ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
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
from AQM_Database.bridge import sync_inventory
from aqm_bridge import run_async
from uuid import UUID
from AQM_Database.bridge import upload_coins, sync_inventory
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer
from AQM_Database.aqm_server import config as srv_config
from AQM_Database.aqm_server.db import create_pool
from AQM_Database.aqm_shared.config import TIER_CEILING




logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aqm.flask")

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--user",    default="alice", help="User identity")
parser.add_argument("--port",    type=int, default=5000)
parser.add_argument("--contacts", nargs="+", default=None,
                    help="Contact usernames e.g. --contacts bob charlie")
parser.add_argument("--contact-ports", nargs="+", type=int, default=None,
                    help="Ports for each contact in same order e.g. --contact-ports 5001 5002")
args, _ = parser.parse_known_args()

USER_ID  = args.user.lower()
PORT     = args.port

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

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.urandom(24)

# ── AQM subsystem init ────────────────────────────────────────────────────────
vault_client     = create_vault_client()
inv_client       = create_inventory_client()
vault            = SecureVault(vault_client)
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
session_store    = SessionStore(db_path=f"{USER_ID}_sessions.db")
crypto           = CryptoEngine()
context_mgr      = ContextManager()

# In-memory active ratchets
active_ratchets: dict[str, SessionRatchet] = {}

# SSE message queue — incoming messages pushed here for the browser
sse_queue: queue.Queue = queue.Queue(maxsize=100)

# Message history (in-memory, per session)
message_history: list[dict] = []

# ── Simulated device context (randomised per message like the CLI demo) ──────
import random

def random_device_context() -> DeviceContext:
    battery = random.uniform(5, 100)
    wifi    = random.choice([True, True, False])
    signal  = random.uniform(-110, -60)
    return DeviceContext(battery_pct=battery, wifi_connected=wifi, signal_dbm=signal)


# ── Bootstrap: mint coins + register partner contact ─────────────────────────
def bootstrap():
    """Mint coins for self, register all known contacts."""
    logger.info("Bootstrapping AQM for user: %s", USER_ID)

    targets = {"GOLD": 5, "SILVER": 6, "BRONZE": 5}
    minted, minted_bundles = 0, []

    for tier, count in targets.items():
        for _ in range(count):
            bundle = crypto.mint_coin(tier)
            minted_bundles.append(bundle)
            vault_key = os.urandom(32)
            blob = crypto.encrypt_aead(bundle.secret_key, vault_key, bundle.key_id.encode())
            iv, auth_tag, enc_blob = blob[:12], blob[-16:], blob[12:-16]
            vault.store_key(
                key_id=bundle.key_id,
                coin_category=bundle.coin_category,
                encrypted_blob=enc_blob,
                encryption_iv=iv,
                auth_tag=auth_tag,
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
                # Start as STRANGER — let frequency drive promotion
                logger.info("Registered %s as STRANGER", cid)
        except Exception as e:
            logger.warning("Could not register contact %s: %s", cid, e)
        try:
            inventory.register_contact(cid, "STRANGER", cid.capitalize())
        except Exception:
            pass

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
                        logger.info("Background sync got coins for %s: %s", cid, fetched)
                        try:
                            sse_queue.put_nowait({"type": "status_update"})
                        except queue.Full:
                            pass
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
    session_store.save_ratchet(r)


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
    try:
        s = vault.get_stats()
        return {
            "active_gold":   s.active_gold,
            "active_silver": s.active_silver,
            "active_bronze": s.active_bronze,
            "total_burned":  s.total_burned,
            "total_expired": s.total_expired,
        }
    except Exception:
        return {"active_gold": 0, "active_silver": 0, "active_bronze": 0,
                "total_burned": 0, "total_expired": 0}


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

@app.route("/")
def index():
    return render_template("index.html",
                           user_id=USER_ID,
                           partner_id=PARTNER_ID,
                           partner_port=PARTNER_PORT,
                           known_contacts=KNOWN_CONTACTS)


@app.route("/api/status")
def api_status():
    counts = coin_counts()
    vstats = vault_stats_dict()
    return jsonify({
        "user_id":    USER_ID,
        "partner_id": PARTNER_ID,
        "coins":      counts,
        "vault":      vstats,
        "contacts":   contacts_list(),
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    data       = request.get_json()
    plaintext  = data.get("message", "").strip()
    contact_id = data.get("contact_id", PARTNER_ID)

    if not plaintext:
        return jsonify({"error": "empty message"}), 400
    if contact_id not in KNOWN_CONTACTS:
        return jsonify({"error": f"unknown contact: {contact_id}"}), 400

    ctx        = random_device_context()
    ideal_tier = context_mgr.select_coin(ctx)

    contact  = contacts_db.get_contact(contact_id)
    priority = contact.priority if contact else "STRANGER"
    ceiling  = TIER_CEILING.get(priority, "BRONZE")

    _TIER_RANK = {"GOLD": 2, "SILVER": 1, "BRONZE": 0}
    tier = ideal_tier if _TIER_RANK.get(ideal_tier, 0) <= _TIER_RANK.get(ceiling, 0) else ceiling

    coin = inventory.select_coin(contact_id, tier)
    
    if coin is None:
        return jsonify({"error": "no coins available"}), 503

    # Get or create ratchet
    ratchet = get_ratchet(contact_id)
    kem_ct, coin_id_used = None, None

    if ratchet is None or ratchet.needs_rekey():
        ct, shared_secret = crypto.kem_encapsulate(coin.public_key, coin.coin_category)
        kem_ct = ct
        if ratchet is None:
            ratchet = SessionRatchet(contact_id, coin.coin_category, shared_secret)
        else:
            ratchet.rekey(shared_secret, coin.coin_category)
        coin_id_used = coin.key_id

    msg_key     = ratchet.derive_message_key()
    aad         = f"{USER_ID}:{contact_id}".encode()
    enc_payload = crypto.encrypt_aead(plaintext.encode(), msg_key, aad)
    save_ratchet(ratchet)


    parcel = {
        "sender_id":         USER_ID,
        "recipient_id":      contact_id,
        "encrypted_payload": base64.b64encode(enc_payload).decode(),
        "aad":               base64.b64encode(aad).decode(),
        "coin_tier":         coin.coin_category,
        "plaintext":         plaintext,          # included for demo so partner can display
        "device_ctx": {
            "battery": round(ctx.battery_pct, 1),
            "wifi":    ctx.wifi_connected,
            "signal":  round(ctx.signal_dbm, 1),
        },
    }
    if coin_id_used:
        parcel["coin_id"]      = coin_id_used
        parcel["kem_ciphertext"] = base64.b64encode(kem_ct).decode()

    # Build message record
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
        "msg_count": ratchet.msg_counter,
        "max_msgs":  ratchet.max_messages,
    }
    message_history.append(msg_record)

    # Record in contacts DB for priority tracking
    try:
        contacts_db.record_message(PARTNER_ID)
    except Exception:
        pass

    # Push to own SSE stream
    try:
        sse_queue.put_nowait({"type": "message", "data": msg_record})
    except queue.Full:
        pass
    try:
        sse_queue.put_nowait({"type": "status_update"})
    except queue.Full:
        pass

    # Forward to partner via HTTP (fire-and-forget)
    # AFTER
    _forward_to_partner(parcel, msg_record, contact_id)

    return jsonify({"ok": True, "message": msg_record, "coins": coin_counts()})


def _forward_to_partner(parcel: dict, msg_record: dict, contact_id: str):
    """POST the parcel to the contact's Flask instance."""
    import urllib.request, urllib.error
    port = CONTACT_PORTS.get(contact_id, PARTNER_PORT)
    partner_url = f"http://localhost:{port}/api/receive"
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
    data       = request.get_json()
    parcel     = data.get("parcel", {})
    msg_record = data.get("msg_record", {})

    # Decrypt if we have a ratchet (best-effort for demo)
    sender = parcel.get("sender_id", "")
    import base64

    ratchet = get_ratchet(sender)
    decrypted_text = parcel.get("plaintext", "")  # fallback: use included plaintext

    if "kem_ciphertext" in parcel and "coin_id" in parcel:
        try:
            coin_id = parcel["coin_id"]
            kem_ct  = base64.b64decode(parcel["kem_ciphertext"])
            entry   = vault.fetch_key(coin_id)
            if entry:
                shared_secret = crypto.kem_decapsulate(kem_ct, entry.encrypted_blob, parcel.get("coin_tier", "BRONZE"))
                coin_tier     = parcel.get("coin_tier", "BRONZE")
                if ratchet is None:
                    ratchet = SessionRatchet(sender, coin_tier, shared_secret)
                else:
                    ratchet.rekey(shared_secret, coin_tier)
                vault.burn_key(coin_id)
        except Exception as e:
            logger.debug("KEM decap failed (expected in demo): %s", e)

    if ratchet:
        try:
            msg_key  = ratchet.derive_message_key()
            aad      = f"{sender}:{USER_ID}".encode()
            enc_data = base64.b64decode(parcel["encrypted_payload"])
            decrypted_text = crypto.decrypt_aead(enc_data, msg_key, aad).decode()
            save_ratchet(ratchet)
        except Exception as e:
            logger.debug("Decrypt failed, using plaintext fallback: %s", e)

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
        "msg_count":  ratchet.msg_counter if ratchet else 0,
        "max_msgs":   ratchet.max_messages if ratchet else 0,
        "incoming":   True,
    }
    message_history.append(incoming)

    try:
        contacts_db.record_message(sender)
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
def api_history():
    return jsonify({"messages": message_history[-100:]})


@app.route("/api/contacts")
def api_contacts():
    return jsonify({"contacts": contacts_list()})


@app.route("/api/contacts/<contact_id>/priority", methods=["POST"])
def api_set_priority(contact_id):
    data     = request.get_json()
    priority = data.get("priority", "STRANGER")
    locked   = data.get("locked", True)
    if locked:
        contacts_db.lock_priority(contact_id, priority)
    else:
        contacts_db.unlock_priority(contact_id)
    return jsonify({"ok": True, "contact": contact_id, "priority": priority})


@app.route("/api/vault")
def api_vault():
    return jsonify(vault_stats_dict())


@app.route("/api/inventory")
def api_inventory():
    return jsonify({"coins": coin_counts(), "partner": PARTNER_ID})

@app.route("/api/debug/server-coins")
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
    logger.info("Starting AQM Flask UI for user=%s on port=%d", USER_ID, PORT)
    logger.info("Partner=%s expected on port=%d", PARTNER_ID, PARTNER_PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
