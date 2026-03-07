# AQM Flask UI

A terminal-aesthetic, real-time web interface for the Amortized Quantum Messaging backend.
Runs two browser tabs simultaneously — one for Alice, one for Bob — each on their own Flask
instance, communicating over local HTTP with SSE for live push.

---

## Architecture

```
Browser (Alice @ :5000)          Browser (Bob @ :5001)
        │                                 │
        │  SSE /stream                    │  SSE /stream
        │  POST /api/send                 │  POST /api/send
        ▼                                 ▼
 Flask app.py (alice)  ──HTTP──▶  Flask app.py (bob)
        │                POST /api/receive       │
        │                                        │
        ▼                                        ▼
  AQM Backend (shared)
  ├── Redis vault      (localhost:6379, db=0)
  ├── Redis inventory  (localhost:6379, db=1)
  ├── PostgreSQL       (localhost:5433)
  ├── ContactsDB       (~/.aqm/<user>_contacts.db)
  └── SessionStore     (<user>_sessions.db)
```

**Message flow:**
1. Alice types a message → Flask calls `inventory.select_coin()` + `crypto.kem_encapsulate()` + `ratchet.derive_message_key()` + `crypto.encrypt_aead()`
2. Flask forwards the encrypted parcel to Bob's `/api/receive` via HTTP
3. Bob's Flask decrypts, burns the vault key, pushes to Bob's SSE stream
4. Both browsers update live coin counters and ratchet state

---

## Prerequisites

All Docker services must be running:
```bash
cd Amortized_Quantum_Messaging
docker compose up -d
```

Conda environment active:
```bash
conda activate aqm-db
```

Flask and flask-cors installed:
```bash
pip install flask flask-cors
```

---

## Running

Open **two terminal tabs**, both with `conda activate aqm-db` and from your project root
(`Amortized_Quantum_Messaging/`):

**Terminal 1 — Alice:**
```bash
python flask_app/app.py --user alice --port 5000 --partner-port 5001
```

**Terminal 2 — Bob:**
```bash
python flask_app/app.py --user bob --port 5001 --partner-port 5000
```

Then open two browser tabs:
- Alice: http://localhost:5000
- Bob:   http://localhost:5001

---

## UI Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  AQM ● │  alice ↔ bob  │ [CONNECTED] │ G:5 S:4 B:1 │ PQ SECURE │
├──────────────┬──────────────────────────┬───────────────────────┤
│  CONTACTS    │  SECURE CHANNEL          │  CRYPTO STATUS        │
│              │                          │                        │
│  Bob         │  [messages with tier     │  Inventory            │
│  BESTIE 🔒   │   badges, device ctx,    │  G: 5  S: 4  B: 1    │
│  G:5 S:4 B:1 │   ratchet counters]      │                        │
│              │                          │  Vault (private keys) │
│  Promotion   │                          │  Burned: N            │
│  progress    │  ─────────────────────   │                        │
│  bars        │  [input] [SEND]          │  Ratchet progress     │
│              │                          │  Device context       │
└──────────────┴──────────────────────────┴───────────────────────┘
```

---

## What each panel shows

### Contacts (left)
- All contacts from the local SQLite contacts DB
- Priority badge: BESTIE (gold) / MATE (silver) / STRANGER (bronze)
- 🔒 icon when priority is manually locked
- Live coin inventory for each contact `[G:x S:x B:x]`
- Promotion progress bars:
  - STRANGER → MATE: messages in last 30 days (threshold: 4)
  - MATE → BESTIE: messages in last 7 days (threshold: 5)
- "↑ MATE SOON / BESTIE SOON" indicator when close to threshold

### Chat (centre)
- Full message history with per-message:
  - **Tier badge** — GOLD / SILVER / BRONZE (colour-coded)
  - Coloured left border matching tier
  - `[n/max]` ratchet counter (e.g. `[3/250]`)
  - `⟳ REKEY` label on messages that triggered a new KEM encapsulation
  - Device context at send time: battery%, WiFi/Cellular, signal dBm
  - Timestamp
- Real-time delivery via SSE (no polling)
- Toast notification on incoming message

### Crypto Status (right)
- **Inventory** — live Gold/Silver/Bronze coin counts with budget caps
- **Vault** — active private keys per tier + total burned count (live flash animation on change)
- **Session Ratchet** — progress bar (messages used / max), tier, counter
- **Last Device Context** — battery, signal, WiFi/Cellular, selected tier

---

## Files

```
flask_app/
├── app.py          # Flask server — all routes, AQM wiring, SSE
├── aqm_bridge.py   # Async-to-sync helper for running AQM coroutines
├── templates/
│   └── index.html  # Single-page UI (HTML + CSS + vanilla JS)
└── README.md       # This file
```

---

## Notes

- **No server required for demo** — inventory is seeded locally with real minted keys so
  the coin lifecycle works without the FastAPI coin server running. To use the real server,
  ensure `uvicorn AQM_Database.aqm_server.api:app --port 8000` is running and call
  `bridge.sync_inventory()` on startup.

- **SQLite DBs** are created automatically at `~/.aqm/<user>_contacts.db` and
  `<user>_sessions.db` in the working directory.

- **Device context** is randomised per message (like the CLI demo), simulating real
  battery/WiFi conditions to demonstrate tier selection.
