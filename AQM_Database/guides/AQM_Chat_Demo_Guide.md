# AQM Chat Demo Guide

## Prerequisites

```bash
conda activate aqm-db
cd AQM_Database && docker compose up -d   # Redis 7 (6379) + PostgreSQL 16 (5433)
```

Verify infrastructure:
```bash
python demo.py --check
```

## Quick Start

### One-Click Demo (Recommended)

Automatically opens two terminal windows — one for Alice, one for Bob:

```bash
python -m AQM_Database.chat.cli --demo-pair
python -m AQM_Database.chat.cli --demo-pair --priority MATE
python demo.py --demo-pair
```

Supports: tmux (split panes), gnome-terminal, konsole, xfce4-terminal, xterm.

### Interactive Two-Terminal Chat (Manual)

**Terminal 1 (Alice):**
```bash
python -m AQM_Database.chat.cli --user alice --partner bob --priority BESTIE
```

**Terminal 2 (Bob):**
```bash
python -m AQM_Database.chat.cli --user bob --partner alice --priority BESTIE
```

Both users auto-provision keys. Once provisioning completes, a chat prompt appears with a live coin counter:

```
  [G:5 S:4 B:1] alice> Hello Bob!
  14:32:01  alice [GOLD] Hello Bob!
           key=a1b2c3d4e5f6…  ctx=Home WiFi, 80% battery  encrypt→publish

  14:32:03  bob [GOLD] Hey Alice!  ✓ burned
           key=x9y8z7w6v5u4…  ctx=Home WiFi, 80% battery  decrypt→verify→burn
```

**Device scenario selection** — prefix your message with `1`, `2`, or `3`:

| Prefix | Scenario | Tier selected |
|--------|----------|---------------|
| `1` (default) | Home WiFi, 80% battery | GOLD |
| `2` | Outdoor cellular, 40% battery | SILVER |
| `3` | Underground, 3% battery | BRONZE |

```
  [G:4 S:4 B:1] alice> 2 Sending from outdoors
  14:32:10  alice [SILVER] Sending from outdoors
           key=...  ctx=Outdoor cellular, 40% battery  encrypt→publish
```

**Commands:**
- `/status` — show remaining coins + vault stats
- `/quit` — end session and clean up

**Features:**
- Real-time message display (incoming messages appear instantly via threaded pub/sub)
- Per-message lifecycle detail (key ID, device context, encrypt→publish / decrypt→verify→burn)
- Tier fallback notification (e.g., "wanted GOLD → fell back to SILVER")
- Live coin counter in the prompt

### Auto Demo (Single Terminal)

Runs all three priority scenarios programmatically:

```bash
python -m AQM_Database.chat.cli --auto
python demo.py --chat
```

### TLS 1.3 Benchmark

```bash
python -m AQM_Database.chat.cli --benchmark
python -m AQM_Database.chat.cli --benchmark --iterations 100
python demo.py --chat-bench
```

## Priority Scenarios

| Priority | Budget (G/S/B) | Behavior |
|----------|----------------|----------|
| BESTIE   | 5/4/1          | All tiers available. Scenario 1→GOLD, 2→SILVER, 3→BRONZE |
| MATE     | 0/6/4          | No GOLD. Scenario 1 (wants GOLD) → falls back to SILVER |
| STRANGER | 0/0/0          | Cannot fetch any coins. All sends return None |

## Device Scenarios

| Scenario | Context | Selected Tier |
|----------|---------|---------------|
| 1        | Home WiFi, 80% battery | GOLD |
| 2        | Outdoor cellular, 40% battery | SILVER |
| 3        | Underground, 3% battery | BRONZE |

## Architecture

```
Alice's Terminal                           Bob's Terminal
┌─────────────────┐                       ┌─────────────────┐
│ Vault (db=0)    │   PostgreSQL Server   │ Vault (db=0)    │
│ Inventory (db=1)│◄─────────────────────►│ Inventory (db=1)│
│ Pub/Sub (db=0)  │   (coin exchange)     │ Pub/Sub (db=0)  │
└────────┬────────┘                       └────────┬────────┘
         │          Redis Pub/Sub                   │
         └──────────────────────────────────────────┘
         aqm:chat:{alice_id}    aqm:chat:{bob_id}
```

## Message Lifecycle (shown per-message in chat)

1. **ContextManager** inspects device state → selects coin tier
2. **SmartInventory.select_coin()** pops oldest coin (FIFO), with fallback to lower tiers
3. **simulate_encrypt()** produces `SHA-256(pk || plaintext) + plaintext` (simulates Kyber KEM + AES-GCM)
4. **ChatTransport.publish()** sends JSON envelope via Redis pub/sub
5. Receiver's subscriber callback:
   - Deserializes ChatMessage
   - Calls **simulate_decrypt()** and verifies integrity tag
   - Calls **vault.fetch_key()** to retrieve private key
   - Calls **vault.burn_key()** — one-time use enforced
   - Displays decrypted message with verification + burn status

## Running Tests

```bash
# Protocol tests only (no Docker)
pytest AQM_Database/chat/tests/test_protocol.py -v

# All chat tests (needs Docker for session + benchmark)
pytest AQM_Database/chat/tests/ -v

# Full project test suite (171 tests)
python demo.py --tests
```

## Troubleshooting

- **"Cannot connect to Redis"** — run `docker compose up -d` in `AQM_Database/`
- **"Partner timeout"** — in interactive mode, both users must start within 120 seconds
- **"No coins available"** — STRANGER has zero budget by design; MATE has no GOLD coins
- **"No supported terminal emulator"** — `--demo-pair` needs gnome-terminal/konsole/xfce4-terminal/xterm/tmux; run manually in two terminals instead
