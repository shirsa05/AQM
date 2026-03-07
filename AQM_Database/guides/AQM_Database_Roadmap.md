# AQM Database Implementation Roadmap

## Overview

This roadmap covers your database workstream across 4 sprints (~8 weeks total). It aligns with the project's overall Phase 1–4 roadmap from the paper but focuses exclusively on the data layer deliverables.

---

## Sprint 1 (Weeks 1–2): Local Foundations

**Goal:** Bob's Secure Vault and Alice's Smart Inventory working locally with Redis, passing all unit tests.

### Week 1: Secure Vault (Bob's Device)

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Set up dev environment: Redis 7+, choose language (Python/TypeScript/Go), install Redis client library | `docker-compose.yml` with Redis, project scaffold |
| 2 | Implement `store_key()` and `fetch_key()` | Core CRUD with Redis Hash |
| 3 | Implement `burn_key()` with two-step atomic burn (set BURNED → schedule DEL) | Burn logic + tests proving burned keys return None |
| 4 | Implement TTL strategy: auto-expire keys after 30 days, `purge_expired()` for manual cleanup | TTL tests with mock timestamps |
| 5 | Implement `count_active()` using SCAN + filter | Inventory counting, full Vault unit test suite green |

### Week 2: Smart Inventory (Alice's Device)

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Design sorted set indexing strategy (`idx:inventory:{contact_id}:{coin_category}`) | Index schema doc |
| 2 | Implement `store_key()` with budget cap enforcement (5G/4S/1B for Bestie, etc.) | Store + reject-on-overflow tests |
| 3 | Implement `select_coin()` with tier fallback logic (Gold → Silver → Bronze) | Selection algorithm + fallback tests |
| 4 | Implement `garbage_collect()` (LRU-based, 30-day inactive threshold) | GC tests with stale data |
| 5 | Implement `set_priority()` and priority-based budget adjustment | Priority reclassification tests, full Inventory test suite green |

### Sprint 1 Exit Criteria
- [ ] `SecureVault` class: all 5 methods implemented and tested
- [ ] `SmartInventory` class: all 6 methods implemented and tested
- [ ] Zero race conditions under concurrent access (test with `asyncio` or goroutines)
- [ ] README documenting interface contracts for teammates

---

## Sprint 2 (Weeks 3–4): Server Database + API Layer

**Goal:** PostgreSQL server with Coin Inventory, Delete-on-Fetch working, HTTP endpoints exposed.

### Week 3: PostgreSQL Schema & Core Operations

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Set up PostgreSQL 16+, create schema with indexes, write migration scripts | `migrations/001_create_coin_inventory.sql` |
| 2 | Implement `upload_coins()` — batch INSERT with duplicate rejection on `(user_id, key_id)` | Upload endpoint + conflict handling tests |
| 3 | Implement `fetch_coins()` — the critical `FOR UPDATE SKIP LOCKED` transaction | Fetch with atomic claim, tested for correctness |
| 4 | Implement `purge_stale()` and `hard_delete_fetched()` background jobs | Cron/scheduler setup, hygiene tests |
| 5 | Implement `get_inventory_count()` for device sync | Count endpoint, full server test suite green |

### Week 4: API & Integration

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Build REST or gRPC API layer wrapping `CoinInventoryServer` | `/upload`, `/fetch`, `/count` endpoints |
| 2 | Add request validation: verify coin_category enum, blob size limits, rate limiting | Input validation + error response tests |
| 3 | Concurrent fetch stress test: 100 goroutines/threads fetching same user's keys | Zero duplicate assignment proof |
| 4 | Wire local databases to server: pre-fetch flow (Smart Inventory calls server `/fetch`) | End-to-end: mint → upload → fetch → store locally |
| 5 | Documentation: API spec (OpenAPI/Protobuf), error codes, example flows | API docs for teammates |

### Sprint 2 Exit Criteria
- [ ] `CoinInventoryServer` class: all 5 methods implemented and tested
- [ ] Delete-on-Fetch: zero duplicates under 100-concurrent-request stress test
- [ ] REST/gRPC API with validation and error handling
- [ ] End-to-end flow: Mint → Upload → Fetch → Local Store working

---

## Sprint 3 (Weeks 5–6): Hardening & SQLite Fallback

**Goal:** Production-ready local databases with SQLite fallback for IoT, encryption at rest, and monitoring.

### Week 5: SQLite Fallback

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Implement `SecureVault` backed by SQLite (same interface, different backend) | SQLite Vault with WAL mode |
| 2 | Implement `SmartInventory` backed by SQLite | SQLite Inventory with composite indexes |
| 3 | Build database backend abstraction: factory that picks Redis or SQLite based on device capability | `DatabaseFactory` with config-driven selection |
| 4 | Run identical test suite against SQLite backends — all must pass | Green test suite for both backends |
| 5 | Benchmark: Redis vs SQLite for typical workloads (100 key lookups, 10 coin selections) | Performance comparison doc |

### Week 6: Security & Observability

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Integrate SQLCipher for encrypting SQLite files (protects contact metadata) | Encrypted SQLite with key derivation |
| 2 | Add Redis AOF persistence configuration, test crash recovery | Persistence config + recovery test |
| 3 | Server: add connection pooling (PgBouncer or built-in pool), query timeouts | Production PostgreSQL config |
| 4 | Add metrics/logging: key operations per second, inventory levels, fetch latency | Prometheus metrics or structured logs |
| 5 | Security review: verify no plaintext private keys in logs, no SQL injection, parameterized queries everywhere | Security checklist doc |

### Sprint 3 Exit Criteria
- [ ] SQLite fallback: identical interface, all tests passing
- [ ] Database factory: auto-selects backend based on config
- [ ] Redis persistence: survives process restart without data loss
- [ ] PostgreSQL: connection pooling, query timeouts, no N+1 queries
- [ ] No security vulnerabilities in database layer

---

## Sprint 4 (Weeks 7–8): Integration & Scale

**Goal:** Full integration with other modules, load testing, and documentation for handoff.

### Week 7: Module Integration

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Integrate with Coin Minting module: they call `store_key()` after generating keys | Verified: minted keys appear in Vault + Server |
| 2 | Integrate with Crypto Wrapper: they call `fetch_key()` during decapsulation | Verified: full decrypt flow using stored keys |
| 3 | Integrate with Context Manager: coin selection uses real battery/signal data | Verified: tier selection works with real context |
| 4 | Integrate with Network Layer: upload/fetch endpoints called by actual device code | Verified: multi-device end-to-end messaging |
| 5 | Bug bash: fix integration issues discovered during full-system testing | All integration tests green |

### Week 8: Load Testing & Documentation

| Day | Task | Deliverable |
|-----|------|-------------|
| 1 | Server load test: 10K concurrent users, measure p50/p95/p99 fetch latency | Load test results + bottleneck analysis |
| 2 | Local performance test: simulate Bestie with 5 contacts, rapid message bursts | Local DB performance report |
| 3 | Write migration playbook: how to add new coin types, change budget caps | Migration guide |
| 4 | Write operational runbook: backup/restore, monitoring alerts, scaling PostgreSQL | Ops runbook |
| 5 | Final documentation: architecture decision records, interface contracts, deployment guide | Complete handoff package |

### Sprint 4 Exit Criteria
- [ ] All modules integrated and communicating through database interfaces
- [ ] Server handles 10K concurrent users with p99 < 50ms for fetch
- [ ] Local DB operations complete in < 5ms for all coin operations
- [ ] Complete documentation package for team handoff

---

## Technology Setup Checklist

### Local Development

```bash
# Redis
docker run -d --name aqm-redis -p 6379:6379 redis:7-alpine

# PostgreSQL
docker run -d --name aqm-postgres -p 5432:5432 \
  -e POSTGRES_DB=aqm -e POSTGRES_PASSWORD=dev \
  postgres:16-alpine

# Full stack
# Create a docker-compose.yml with both + your app
```

### Recommended Libraries by Language

| Language | Redis Client | PostgreSQL Client | SQLite |
|----------|-------------|-------------------|--------|
| **Python** | `redis-py` + `hiredis` | `asyncpg` (async) or `psycopg3` | `sqlite3` (stdlib) + `sqlcipher3` |
| **TypeScript** | `ioredis` | `pg` + `@types/pg` | `better-sqlite3` |
| **Go** | `go-redis/redis` | `pgx` | `mattn/go-sqlite3` |
| **Rust** | `redis-rs` | `tokio-postgres` | `rusqlite` |

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Redis data loss on device crash | Lost private keys = lost messages | Enable AOF with `appendfsync everysec`; SQLite fallback |
| Double-fetch on server (same key to two senders) | Security breach — message can be decrypted by wrong party | `FOR UPDATE SKIP LOCKED` + integration test proving zero duplicates |
| Budget cap bypass under race condition | More keys cached than intended, wasting device storage | Use Redis `WATCH/MULTI/EXEC` or Lua script for atomic check-and-store |
| PostgreSQL single point of failure | Server downtime = no new key fetches (cached keys still work) | Phase 4: evaluate CockroachDB for HA; short-term: streaming replication |
| Schema migration breaks running devices | App crashes on old schema | Version your key prefixes (Redis) and use `IF NOT EXISTS` (SQL) |
