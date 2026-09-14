"""Unit tests for ucp4_client.py's wire framing (pack/unpack records)."""
import json
import os
import struct
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ucp4_client  # noqa: E402


class TestPackRecord(unittest.TestCase):
    def test_header_shape(self):
        payload = {"a": 1, "b": [1, 2, 3]}
        rec = ucp4_client.pack_record(3, payload)
        rtype, version, reserved, length = struct.unpack(">BB4sH", rec[:8])
        self.assertEqual(rtype, 3)
        self.assertEqual(version, 1)
        self.assertEqual(reserved, b"\x00\x00\x00\x00")
        body = rec[8:]
        self.assertEqual(length, len(body))
        self.assertEqual(json.loads(body), payload)

    def test_length_is_body_only(self):
        rec = ucp4_client.pack_record(9, {})
        self.assertEqual(struct.unpack(">H", rec[6:8])[0], 2)  # len("{}")

    def test_pack_message_is_head_then_body(self):
        data = ucp4_client.pack_message({"h": 1}, {"b": 2})
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual([r[0] for r in recs], [1, 2])
        self.assertEqual(recs[0][2], {"h": 1})
        self.assertEqual(recs[1][2], {"b": 2})


class TestUnpackAllRecords(unittest.TestCase):
    def test_roundtrip_multiple(self):
        data = (ucp4_client.pack_record(1, {"x": "y"})
                + ucp4_client.pack_record(2, {"n": 42})
                + ucp4_client.pack_record(3, {"l": [1, 2]}))
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual([r[0] for r in recs], [1, 2, 3])
        self.assertEqual([r[2] for r in recs], [{"x": "y"}, {"n": 42}, {"l": [1, 2]}])
        self.assertTrue(all(r[1] == 1 for r in recs))

    def test_empty(self):
        self.assertEqual(ucp4_client.unpack_all_records(b""), [])

    def test_non_json_payload_is_raw(self):
        body = b"not json"
        data = struct.pack(">BB4sH", 7, 1, b"\x00\x00\x00\x00", len(body)) + body
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual(recs, [(7, 1, {"_raw": "not json"})])

    def test_trailing_partial_header_ignored(self):
        data = ucp4_client.pack_record(1, {"a": 1}) + b"\x01\x02\x03"
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0][2], {"a": 1})


if __name__ == "__main__":
    unittest.main()
