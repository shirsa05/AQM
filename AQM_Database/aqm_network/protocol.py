import json
import base64
from json import JSONDecodeError

from AQM_Database.aqm_shared import config


MAX_MESSAGE_BYTES = 65_536  # 64 KB ceiling for raw wire messages


def frame_message(msg_type: str, payload_dict: dict) -> str:
    # wraps outgoing message
    my_pay_load = payload_dict.copy()
    if msg_type not in config.MESSAGE_TYPE:
        raise ValueError(f"Invalid message type on frame{msg_type}")

    for key, value in list(payload_dict.items()):
        if isinstance(value, bytes):
            my_pay_load[key] = base64.b64encode(value).decode("utf-8")

    my_pay_load["msg_type"] = msg_type
    return json.dumps(my_pay_load)


def parse_message(raw: str) -> tuple[str, dict[str, object]]:
    # unwraps incoming
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError(f"Message too large: {len(raw)} bytes (max {MAX_MESSAGE_BYTES})")

    try:
        payload_dict = json.loads(raw)
        if "msg_type" not in payload_dict:
            raise ValueError("Missing msg_type field")

        if payload_dict["msg_type"] not in config.MESSAGE_TYPE:
            raise ValueError(f"Invalid message type on parse {payload_dict['msg_type']}")

        msg_type = payload_dict.pop("msg_type")
        return msg_type, payload_dict
    except JSONDecodeError:
        raise ValueError("Invalid JSON on decode")
