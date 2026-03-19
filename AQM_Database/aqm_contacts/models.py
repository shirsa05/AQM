from dataclasses import dataclass

@dataclass
class Contact:
    contact_id: str
    display_name: str
    priority : str
    public_signing_key : bytes | None
    first_seen_at : str
    last_msg_at : str | None
    msg_count_total: int
    msg_count_7d: int
    msg_count_30d: int
    priority_locked: bool
    is_blocked: bool
