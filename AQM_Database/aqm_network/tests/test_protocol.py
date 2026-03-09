import json
import pytest
from AQM_Database.aqm_network.protocol import frame_message, parse_message, MAX_MESSAGE_BYTES


# ── frame_message ──


class TestFrameMessage:
    def test_basic_frame(self):
        result = frame_message("AUTH", {"user_id": "alice"})
        parsed = json.loads(result)
        assert parsed["msg_type"] == "AUTH"
        assert parsed["user_id"] == "alice"

    def test_bytes_are_base64_encoded(self):
        payload = {"data": b"\x00\x01\x02\xff"}
        result = frame_message("PARCEL", payload)
        parsed = json.loads(result)
        assert isinstance(parsed["data"], str)
        # original dict should NOT be mutated
        assert isinstance(payload["data"], bytes)

    def test_original_dict_not_mutated(self):
        payload = {"user_id": "alice"}
        frame_message("AUTH", payload)
        assert "msg_type" not in payload

    def test_invalid_msg_type_raises(self):
        with pytest.raises(ValueError, match="Invalid message type"):
            frame_message("BOGUS", {"user_id": "alice"})

    def test_empty_payload(self):
        result = frame_message("ACK", {})
        parsed = json.loads(result)
        assert parsed["msg_type"] == "ACK"


# ── parse_message ──


class TestParseMessage:
    def test_basic_parse(self):
        raw = json.dumps({"msg_type": "AUTH", "user_id": "alice"})
        msg_type, payload = parse_message(raw)
        assert msg_type == "AUTH"
        assert payload["user_id"] == "alice"
        assert "msg_type" not in payload

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_message("not json{{{")

    def test_missing_msg_type_raises(self):
        raw = json.dumps({"user_id": "alice"})
        with pytest.raises(ValueError, match="Missing msg_type"):
            parse_message(raw)

    def test_invalid_msg_type_raises(self):
        raw = json.dumps({"msg_type": "BOGUS"})
        with pytest.raises(ValueError, match="Invalid message type"):
            parse_message(raw)

    def test_oversized_message_raises(self):
        raw = json.dumps({"msg_type": "AUTH", "data": "x" * MAX_MESSAGE_BYTES})
        with pytest.raises(ValueError, match="too large"):
            parse_message(raw)


# ── roundtrip ──


class TestRoundtrip:
    def test_frame_then_parse(self):
        original = {"user_id": "bob", "extra": "data"}
        framed = frame_message("AUTH", original)
        msg_type, payload = parse_message(framed)
        assert msg_type == "AUTH"
        assert payload["user_id"] == "bob"
        assert payload["extra"] == "data"

    def test_roundtrip_with_bytes(self):
        original = {"recipient_id": "bob", "data": b"\xde\xad\xbe\xef"}
        framed = frame_message("PARCEL", original)
        msg_type, payload = parse_message(framed)
        assert msg_type == "PARCEL"
        # data arrives as base64 string, not bytes — that's by design
        assert isinstance(payload["data"], str)
