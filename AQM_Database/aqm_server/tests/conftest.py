import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4

from AQM_Database.aqm_shared.types import CoinUpload
from AQM_Database.aqm_server.coin_inventory import CoinInventoryServer

TEST_DSN = os.environ.get(
    "AQM_TEST_DSN",
    "postgresql://aqm_user:aqm_dev_password@localhost:5433/aqm_test",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS coin_inventory(
    record_id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    key_id VARCHAR(36) NOT NULL,

    coin_category VARCHAR(6) NOT NULL CHECK ( coin_category IN ('GOLD' , 'SILVER' , 'BRONZE') ),
    public_key_blob BYTEA NOT NULL,
    signature_blob BYTEA NOT NULL,

    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_by UUID DEFAULT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NULL,

    CONSTRAINT uq_user_key UNIQUE (user_id , key_id)
);

CREATE INDEX IF NOT EXISTS idx_coin_lookup
    ON coin_inventory (user_id , coin_category , uploaded_at ASC)
    WHERE fetched_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_coin_expiry
    ON coin_inventory (uploaded_at)
    WHERE fetched_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_coin_hard_delete
    ON coin_inventory (fetched_at)
    WHERE fetched_by IS NOT NULL;
"""


@pytest_asyncio.fixture(scope="session")
async def _create_test_db():
    """Create aqm_test database if it doesn't exist."""
    sys_dsn = TEST_DSN.rsplit("/", 1)[0] + "/aqm"
    conn = await asyncpg.connect(sys_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'aqm_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE aqm_test")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def pool(_create_test_db):
    """Session-scoped connection pool; creates schema once."""
    p = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=20)
    async with p.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    yield p
    await p.close()


@pytest_asyncio.fixture(autouse=True)
async def _truncate(pool):
    """Truncate table before every test for isolation."""
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE coin_inventory RESTART IDENTITY")


@pytest.fixture
def inventory(pool) -> CoinInventoryServer:
    return CoinInventoryServer(pool)


@pytest.fixture
def bob_id():
    return uuid4()


@pytest.fixture
def alice_id():
    return uuid4()


def make_coins(n: int, category: str = "SILVER") -> list[CoinUpload]:
    """Helper: generate n coins with random blobs."""
    sizes = {"GOLD": (1184, 2420), "SILVER": (1184, 64), "BRONZE": (32, 64)}
    pk_size, sig_size = sizes[category]
    return [
        CoinUpload(
            key_id=str(uuid4()),
            coin_category=category,
            public_key_blob=os.urandom(pk_size),
            signature_blob=os.urandom(sig_size),
        )
        for _ in range(n)
    ]
