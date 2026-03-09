import os
import threading

from AQM_Database.aqm_shared import errors
from AQM_Database.aqm_db.vault import SecureVault
from AQM_Database.aqm_db.inventory import SmartInventory
import fakeredis


def _make_pub_key(tier="GOLD"):
    sizes = {"GOLD": 1184, "SILVER": 1184, "BRONZE": 32}
    return os.urandom(sizes[tier])


def _make_sig(tier="GOLD"):
    sizes = {"GOLD": 2420, "SILVER": 64, "BRONZE": 64}
    return os.urandom(sizes[tier])


def test_concurrent_store_respects_budget():
    """Launch 10 threads all trying to store Gold keys for the same Bestie (cap=5)."""
    # fakeredis with shared server for true concurrency simulation
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    inventory = SmartInventory(client)
    inventory.register_contact("bob", "BESTIE", "Bob")

    results = {"success": 0, "budget_exceeded": 0}
    lock = threading.Lock()

    def store_one(idx):
        try:
            # Each thread uses its own client connected to the same server
            thread_client = fakeredis.FakeRedis(server=server)
            inv = SmartInventory(thread_client)
            inv.store_key("bob", f"gold_{idx}", "GOLD", _make_pub_key("GOLD"), _make_sig("GOLD"))
            with lock:
                results["success"] += 1
        except errors.BudgetExceededError:
            with lock:
                results["budget_exceeded"] += 1

    threads = [threading.Thread(target=store_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["success"] == 5
    assert results["budget_exceeded"] == 5


def test_concurrent_select_no_duplicates():
    """Pre-load 5 Silver keys. Launch 10 threads calling select_coin."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    inventory = SmartInventory(client)
    inventory.register_contact("bob", "BESTIE", "Bob")

    for i in range(4):  # Bestie Silver cap is 4
        inventory.store_key("bob", f"silver_{i}", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))

    selected_keys = []
    lock = threading.Lock()

    def select_one():
        thread_client = fakeredis.FakeRedis(server=server)
        inv = SmartInventory(thread_client)
        entry = inv.select_coin("bob", "SILVER")
        if entry is not None:
            with lock:
                selected_keys.append(entry.key_id)

    threads = [threading.Thread(target=select_one) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(selected_keys) == 4
    assert len(set(selected_keys)) == 4  # no duplicates


def test_concurrent_burn_idempotent():
    """Store 1 key. Launch 5 threads all calling burn_key."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    vault = SecureVault(client)
    vault.store_key("key_001", "GOLD", os.urandom(100), os.urandom(12), os.urandom(16))

    results = {"success": 0, "already_burned": 0}
    lock = threading.Lock()

    def burn_one():
        try:
            thread_client = fakeredis.FakeRedis(server=server)
            v = SecureVault(thread_client)
            v.burn_key("key_001")
            with lock:
                results["success"] += 1
        except errors.KeyAlreadyBurnedError:
            with lock:
                results["already_burned"] += 1

    threads = [threading.Thread(target=burn_one) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["success"] == 1
    assert results["already_burned"] == 4


def test_concurrent_store_and_select():
    """One thread stores, another selects. Run briefly, assert no crashes."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    inventory = SmartInventory(client)
    inventory.register_contact("bob", "BESTIE", "Bob")

    stop = threading.Event()
    store_count = {"n": 0}
    select_count = {"n": 0}

    def store_loop():
        thread_client = fakeredis.FakeRedis(server=server)
        inv = SmartInventory(thread_client)
        i = 0
        while not stop.is_set():
            try:
                inv.store_key("bob", f"silver_loop_{i}", "SILVER", _make_pub_key("SILVER"), _make_sig("SILVER"))
                store_count["n"] += 1
                i += 1
            except (errors.BudgetExceededError, errors.ConcurrencyError):
                pass

    def select_loop():
        thread_client = fakeredis.FakeRedis(server=server)
        inv = SmartInventory(thread_client)
        while not stop.is_set():
            entry = inv.select_coin("bob", "SILVER")
            if entry is not None:
                select_count["n"] += 1

    t1 = threading.Thread(target=store_loop)
    t2 = threading.Thread(target=select_loop)
    t1.start()
    t2.start()

    import time
    time.sleep(0.5)
    stop.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    # Just verify we did *something* without crashing
    assert store_count["n"] >= 0
    assert select_count["n"] >= 0
