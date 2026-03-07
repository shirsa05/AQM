import sqlite3
import json
from pathlib import Path
from typing import Optional
from .ratchet import SessionRatchet

class SessionStore:
    
    def __init__(self, db_path: str = "aqm_sessions.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Creates the sessions table if it does not exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ratchets (
                    contact_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    def save_ratchet(self, ratchet: SessionRatchet) -> None:
        
        state_dict = ratchet.get_state()
        state_json = json.dumps(state_dict)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO ratchets (contact_id, state_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(contact_id) DO UPDATE SET 
                    state_json=excluded.state_json,
                    updated_at=CURRENT_TIMESTAMP
            ''', (ratchet.contact_id, state_json))
            conn.commit()

    def load_ratchet(self, contact_id: str) -> Optional[SessionRatchet]:
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT state_json FROM ratchets WHERE contact_id = ?', 
                (contact_id,)
            )
            row = cursor.fetchone()

            if row:
                state_dict = json.loads(row[0])
                return SessionRatchet.from_state(state_dict)
            
            return None

    def delete_ratchet(self, contact_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM ratchets WHERE contact_id = ?', (contact_id,))
            conn.commit()
