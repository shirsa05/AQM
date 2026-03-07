import redis
from AQM_Database.aqm_shared import errors, config
from AQM_Database.aqm_shared.types import HealthStatus



def create_vault_client() -> redis.Redis:
    r = redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_VAULT_DB,
        decode_responses=False,
        socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT,
        socket_timeout=config.REDIS_SOCKET_TIMEOUT,
    )
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        raise errors.VaultUnavailableError(f"Cannot connect to Redis at {config.REDIS_HOST}:{config.REDIS_PORT}")
    return r


def create_inventory_client() -> redis.Redis:
    r = redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_INVENTORY_DB,
        decode_responses=False,
        socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT,
        socket_timeout=config.REDIS_SOCKET_TIMEOUT,
    )
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        raise errors.InventoryUnavailableError(f"Cannot connect to Redis at {config.REDIS_HOST}:{config.REDIS_PORT}")
    return r


def health_check(vault_client, inventory_client) -> HealthStatus:
    vault_ok = False
    inv_ok = False
    vault_keys = 0
    inv_keys = 0
    uptime = 0.0

    try:
        vault_ok = vault_client.ping()
        vault_keys = vault_client.dbsize()
        uptime += vault_client.info().get('uptime_in_seconds', 0)
    except redis.exceptions.ConnectionError:
        pass

    try:
        inv_ok = inventory_client.ping()
        inv_keys = inventory_client.dbsize()
        uptime += inventory_client.info().get('uptime_in_seconds', 0)
    except redis.exceptions.ConnectionError:
        pass

    return HealthStatus(
        vault_connected=vault_ok,
        inventory_connected=inv_ok,
        vault_key_count=vault_keys,
        inventory_key_count=inv_keys,
        uptime_seconds=uptime,
    )


def close_all(vault_client, inventory_client) -> None:
    vault_client.close()
    inventory_client.close()