import asyncpg
import asyncio

from AQM_Database.aqm_shared.errors import ConnectionPoolError

pool: asyncpg.Pool = None

async def create_pool(dsn: str, min_size: int = 5, max_size: int = 20) -> asyncpg.Pool:
    global pool
    if pool is not None:
        return pool

    try:
        pool = await asyncpg.create_pool(
            dsn=dsn,
            max_size=max_size,
            min_size=min_size,
        )
    except (asyncpg.PostgresError, OSError) as e:
        raise ConnectionPoolError(f"Failed to create pool: {e}")

    return pool

async def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise ConnectionPoolError("Pool not initialized. Call create_pool() first.")
    return pool

async def close_pool() -> None:
    global pool
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=5.0)
    except asyncio.TimeoutError:
        pool.terminate()
    finally:
        pool = None

async def health_check() -> bool:
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            return result == 1
    except Exception:
        return False
