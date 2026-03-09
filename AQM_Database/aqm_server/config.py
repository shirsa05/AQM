# PostgreSQL Connection
PG_DSN = "postgresql://aqm_user:aqm_dev_password@localhost:5433/aqm"

# Connection Pool
PG_POOL_MIN_SIZE = 5
PG_POOL_MAX_SIZE = 20

# Maintenance
PURGE_STALE_MAX_AGE_DAYS = 30
HARD_DELETE_GRACE_HOURS = 1

# Test Database
PG_TEST_DSN = "postgresql://aqm_user:aqm_dev_password@localhost:5433/aqm_test"
