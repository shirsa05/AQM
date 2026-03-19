"""Fixtures for ContactsDatabase tests.

Uses SQLite :memory: — no files, no Docker, instant teardown.
Each test gets a fresh database via the `db` fixture.
"""

import pytest
from AQM_Database.aqm_contacts.contacts_db import ContactsDatabase


@pytest.fixture
def db(tmp_path):
    """Fresh ContactsDatabase backed by a temp-dir SQLite file.

    We use tmp_path (not :memory:) because ContactsDatabase.__init__
    calls os.makedirs on the parent directory.  tmp_path gives a real
    filesystem path that pytest cleans up automatically.
    """
    db_path = str(tmp_path / "test_contacts.db")
    return ContactsDatabase(db_path=db_path)
