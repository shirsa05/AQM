import pytest
import os

@pytest.fixture
def dummy_master_secret() -> bytes:
    """Provides a dummy 32-byte shared secret for testing."""
    return os.urandom(32)

@pytest.fixture
def contact_alice() -> str:
    """Provides a dummy contact UUID."""
    return "alice-uuid-1234-5678"

@pytest.fixture
def session_db_path(tmp_path) -> str:
    """Provides an isolated, temporary SQLite DB path so tests don't overwrite real data."""
    return str(tmp_path / "test_sessions.db")
