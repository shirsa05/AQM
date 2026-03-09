import sqlite3
import os

from AQM_Database.aqm_contacts.models import Contact
from datetime import datetime
from dataclasses import dataclass, astuple, fields
from AQM_Database.aqm_shared import config

class ContactsDatabase:
    def __init__(self, db_path: str = "~/.aqm/contacts.db"):
        db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self.connection.execute('PRAGMA foreign_keys = ON')
        self.cursor = self.connection.cursor()

        self.cursor.execute("""
                       CREATE TABLE IF NOT EXISTS contacts (
                                                 contact_id TEXT PRIMARY KEY,
                                                 display_name TEXT NOT NULL,
                                                 priority TEXT NOT NULL DEFAULT 'STRANGER' CHECK ( priority in ('BESTIE' , 'MATE' , 'STRANGER') ),
                                                 public_signing_key BLOB,
                                                 first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                                 last_msg_at TIMESTAMP,
                                                 msg_count_total    INTEGER DEFAULT 0,
                                                 msg_count_7d       INTEGER DEFAULT 0, -- rolling 7-day message count
                                                 msg_count_30d      INTEGER DEFAULT 0, -- rolling 30-day message count
                                                 priority_locked    BOOLEAN DEFAULT 0, -- manual override (user pins priority)
                                                 is_blocked         BOOLEAN DEFAULT 0
        
                       )""")

        #The log stores one row per message. Rolling counts are recalculated from it.
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS message_log(
                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                      contact_id TEXT NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
                                                      timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                          )
                       """)

        self.cursor.executescript("""
                       CREATE INDEX IF NOT EXISTS idx_priority ON contacts (priority);
                       CREATE INDEX IF NOT EXISTS idx_last_msg ON contacts (last_msg_at);
                       CREATE INDEX IF NOT EXISTS idx_msg_log_ts ON message_log (contact_id , timestamp);
                       """)

        self.connection.commit()

    def add_contact(self , contact_id : str , display_name : str , signing_key : bytes = None) -> Contact:
        contact = Contact(contact_id = contact_id,
                          display_name = display_name,
                          priority='STRANGER',
                          public_signing_key=signing_key,
                          first_seen_at=datetime.now(),
                          last_msg_at=None,
                          msg_count_total=0,
                          msg_count_7d=0,
                          msg_count_30d=0,
                          priority_locked=False,
                          is_blocked=False
                          )

        contact_tuple = astuple(contact)
        field_names = [field.name for field in fields(Contact)]
        columns = ', '.join(field_names)
        placeholders = ', '.join(['?'] * len(field_names))
        table_name = 'contacts'

        insert_query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders}) ON CONFLICT(contact_id) DO UPDATE SET display_name=excluded.display_name"

        self.cursor.execute(insert_query, contact_tuple)
        self.connection.commit()

        return contact

    def remove_contact(self , contact_id : str ) -> bool:
        if contact_id is None:
            return False

        self.cursor.execute("""DELETE FROM contacts WHERE contact_id = ?""", (contact_id,))
        self.connection.commit()

        if self.cursor.rowcount > 0:
            return True
        else:
            return False

    def get_contact(self , contact_id : str) -> Contact | None:
        if contact_id is None:
            return None

        self.cursor.execute("""SELECT * FROM contacts WHERE contact_id = ?""", (contact_id,))
        contact = self.cursor.fetchone()
        if contact:
            return Contact(*contact)
        else:
            return None

    def _extract_contacts(self, contacts) -> list[Contact] | None:
        return [Contact(*row) for row in contacts]


    def get_contacts_by_priority(self , priority:str) -> list[Contact] | None:
        if priority not in config.VALID_PRIORITIES:
            return None

        self.cursor.execute("""SELECT * FROM contacts WHERE priority = ?""", (priority,))
        contacts = self.cursor.fetchall()
        return self._extract_contacts(contacts)

    def get_all_contacts(self) -> list[Contact] | None:
        self.cursor.execute("""SELECT * FROM contacts""")
        contacts = self.cursor.fetchall()
        return self._extract_contacts(contacts)

    def record_message(self , contact_id) -> Contact:
        time = datetime.now()
        self.cursor.execute("""INSERT INTO message_log (contact_id, timestamp) VALUES (?, ?)""", (contact_id,time))
        self.cursor.execute("""UPDATE contacts SET msg_count_total = msg_count_total+1 , last_msg_at = ? WHERE contact_id = ?""", (time , contact_id))

        #recalculating rolling counts
        self.cursor.execute("""
        UPDATE contacts SET
            msg_count_7d  = (SELECT COUNT(*) FROM message_log
                             WHERE contact_id = ? AND timestamp > datetime('now', '-7 days')),
            msg_count_30d = (SELECT COUNT(*) FROM message_log
                             WHERE contact_id = ? AND timestamp > datetime('now', '-30 days'))
        WHERE contact_id = ?
        """, (contact_id , contact_id , contact_id))

        self.connection.commit()
        self._recompute_priority(contact_id)
        return self.get_contact(contact_id)

    def _recompute_priority(self , contact_id) -> str | None:
        contact : Contact | None = self.get_contact(contact_id)
        if not contact:
            return None
        if contact.priority_locked:
            return None # user pinned

        if contact.msg_count_7d >= config.CONTACT_THRESHOLDS["BESTIE_THRESHOLD_7D"]:
            new_priority = "BESTIE"
        elif contact.msg_count_30d >= config.CONTACT_THRESHOLDS["MATE_THRESHOLD_30D"]:
            new_priority = "MATE"
        else:
            new_priority = "STRANGER"

        if new_priority != contact.priority:
            self.cursor.execute("""UPDATE contacts SET priority = ? WHERE contact_id = ?""", (new_priority, contact_id))
            self.connection.commit()
            return new_priority
        return None

    def refresh_rolling_counts(self) -> int:
        self.cursor.execute("""DELETE FROM message_log WHERE timestamp < datetime('now', '-30 days')""")

        Contacts = self.get_all_contacts()
        updates = 0
        for contact in Contacts:
            cid = contact.contact_id
            self.cursor.execute("""
            UPDATE contacts SET
                msg_count_7d  = (SELECT COUNT(*) FROM message_log
                                 WHERE contact_id = ? AND timestamp > datetime('now', '-7 days')),
                msg_count_30d = (SELECT COUNT(*) FROM message_log
                                 WHERE contact_id = ? AND timestamp > datetime('now', '-30 days'))
            WHERE contact_id = ?
            """, (cid, cid, cid))

            if self._recompute_priority(cid) is not None:
                updates += 1

        self.connection.commit()
        return updates

    def lock_priority(self , contact_id , priority) -> Contact | None:
        if contact_id is None:
            return None
        if priority not in config.VALID_PRIORITIES:
            return None
        self.cursor.execute("""UPDATE contacts SET priority = ? , priority_locked = True WHERE contact_id = ?""", (priority , contact_id,))
        self.connection.commit()
        return self.get_contact(contact_id)

    def unlock_priority(self , contact_id) -> Contact | None:
        if contact_id is None:
            return None
        self.cursor.execute("""UPDATE contacts SET priority_locked = False WHERE contact_id = ?""", (contact_id,))
        self.connection.commit()
        return self.get_contact(contact_id)


    def get_inactive_contacts(self , days = 30) -> list[Contact] | None:
        modifier = f'-{days} days'
        self.cursor.execute("""SELECT * FROM contacts WHERE last_msg_at < datetime('now', ?) OR last_msg_at IS NULL""" , (modifier,))
        contacts = self.cursor.fetchall()
        return self._extract_contacts(contacts)

    def block_contact(self , contact_id) -> None:
        self.cursor.execute("""UPDATE contacts SET is_blocked = True WHERE contact_id = ?""", (contact_id,))
        self.connection.commit()

    def search_contact(self , query) -> list[Contact]:
        modifier = f'{query}%'
        self.cursor.execute("""SELECT * FROM contacts WHERE display_name LIKE ?""" , (modifier,))
        contacts = self.cursor.fetchall()
        return self._extract_contacts(contacts)



