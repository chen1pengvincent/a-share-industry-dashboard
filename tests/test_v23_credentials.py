from __future__ import annotations

import json
import sys
import tempfile
import traceback
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from swivd.tushare_client import (
    ENDPOINT_FIELDS,
    SecureTushareClient,
    TushareApiError,
    TushareProtocolError,
    decode_rows,
)
from swivd.v2_provider import V2TushareProvider


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, raw: bytes):
        self.raw = raw
        self.closed = False

    def geturl(self):
        return "https://api.tushare.pro"

    def read(self):
        return self.raw

    def close(self):
        self.closed = True


class CredentialReflectionTests(unittest.TestCase):
    def setUp(self):
        # Explicitly fake; all response variants are generated only in memory.
        self.credential = "SYNTHETIC_CANARY_" + "CREDENTIAL_REFLECTION_V23"
        self.params = {"start_date": "20211213", "end_date": "20211213"}
        for target in ("load_tushare_token", "build_opener"):
            patcher = mock.patch(
                f"swivd.tushare_client.{target}",
                side_effect=AssertionError("test must use its injected credential and transport"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def client_for(self, raw):
        response = Response(raw)
        attempts = []

        def open_response(_request, *, timeout):
            attempts.append(timeout)
            return response

        client = SecureTushareClient(
            token=self.credential,
            opener=open_response,
            sleep=lambda _delay: self.fail("credential failures must not be retried"),
        )
        return client, response, attempts

    def encoded_payload(self, payload, *, mixed=False):
        raw = json.dumps(payload, ensure_ascii=False)
        escaped = (
            f"\\u{ord(self.credential[0]):04x}" + self.credential[1:]
            if mixed
            else "".join(f"\\u{ord(character):04x}" for character in self.credential)
        )
        raw = raw.replace(self.credential, escaped).encode("utf-8")
        self.assertNotIn(self.credential.encode("utf-8"), raw)
        return raw

    def assert_rejected(self, raw):
        client, response, attempts = self.client_for(raw)
        with self.assertRaises(TushareProtocolError) as raised:
            client.call("trade_cal", self.params)
        error = raised.exception
        self.assertEqual(str(error), "response contains credential material")
        self.assertEqual(error.args, ("response contains credential material",))
        self.assertEqual(vars(error), {})
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(self.credential, "".join(traceback.format_exception(error)))
        self.assertTrue(response.closed)
        self.assertEqual(len(attempts), 1)

    def test_unicode_escaped_message_and_mixed_encoding_are_rejected(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                self.assert_rejected(self.encoded_payload(
                    {"code": 0, "msg": "prefix " + self.credential + " suffix"},
                    mixed=mixed,
                ))

    def test_unicode_escaped_object_keys_and_values_are_rejected(self):
        payloads = {
            "root_key": {self.credential: None},
            "nested_key": {"metadata": {"prefix " + self.credential: 1}},
            "nested_value": {"metadata": {"detail": self.credential}},
        }
        for location, payload in payloads.items():
            with self.subTest(location=location):
                self.assert_rejected(self.encoded_payload({"code": 0, **payload}))

    def test_unicode_escaped_fields_cells_and_nested_arrays_are_rejected(self):
        payloads = {
            "fields": {"data": {"fields": ["exchange", self.credential], "items": []}},
            "cell": {"data": {"fields": ["exchange"], "items": [[self.credential]]}},
            "nested_array": {"metadata": [None, False, 1, [[self.credential]]]},
            "object_in_array": {"metadata": [[{self.credential: "value"}]]},
        }
        for location, payload in payloads.items():
            with self.subTest(location=location):
                self.assert_rejected(self.encoded_payload({"code": 0, **payload}))

    def test_nonzero_api_code_does_not_bypass_credential_rejection(self):
        self.assert_rejected(self.encoded_payload(
            {"code": 2002, "msg": self.credential, "data": None}
        ))

    def test_overwritten_duplicate_values_are_checked_before_dict_conversion(self):
        escaped = self.encoded_payload(self.credential).decode("utf-8")
        payloads = {
            "root_value": '{"code":0,"msg":' + escaped + ',"msg":"ok"}',
            "nested_value": '{"code":0,"data":{"detail":' + escaped + ',"detail":null}}',
            "discarded_array": '{"code":0,"metadata":[[' + escaped + ']],"metadata":null}',
            "discarded_object": '{"code":0,"metadata":{' + escaped + ':null},"metadata":null}',
        }
        for location, payload in payloads.items():
            with self.subTest(location=location):
                raw = payload.encode("utf-8")
                self.assertNotIn(self.credential.encode("utf-8"), raw)
                self.assertNotIn(self.credential, repr(json.loads(raw)))
                self.assert_rejected(raw)

    def test_existing_plaintext_scan_still_rejects_before_json_parsing(self):
        self.assert_rejected(("not JSON: " + self.credential).encode("utf-8"))

    def test_normal_response_preserves_raw_bytes_decimal_and_duplicate_semantics(self):
        raw = (
            b' {"code":0,"msg":"earlier","msg":"\\u6b63\\u5e38",'
            b'"metadata":{"value":1.2300,"empty":null,"flags":[true,false]},'
            b'"data":{"fields":["exchange","cal_date","is_open","pretrade_date"],'
            b'"items":[["SSE","20211213",1,"20211210"]]}}\n'
        )
        client, response, attempts = self.client_for(raw)
        result = client.call("trade_cal", self.params)
        self.assertIs(result.raw_bytes, raw)
        self.assertEqual(result.raw_text, raw.decode("utf-8"))
        self.assertEqual(result["msg"], "正常")
        self.assertEqual(str(result["metadata"]["value"]), "1.2300")
        self.assertIsInstance(result["metadata"]["value"], Decimal)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.headers, response.headers)
        self.assertEqual(result.api_name, "trade_cal")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(decode_rows(result, api_name="trade_cal"), [
            dict(zip(ENDPOINT_FIELDS["trade_cal"], ["SSE", "20211213", 1, "20211210"]))
        ])
        self.assertTrue(response.closed)
        self.assertEqual(len(attempts), 1)

    def test_noncredential_api_error_retains_existing_call_and_decode_behavior(self):
        raw = b'{"code":2002,"msg":"ordinary upstream error","data":null}'
        client, _, _ = self.client_for(raw)
        result = client.call("trade_cal", self.params)
        self.assertIs(result.raw_bytes, raw)
        with self.assertRaises(TushareApiError) as raised:
            decode_rows(result, api_name="trade_cal")
        self.assertEqual(raised.exception.code, 2002)
        self.assertIs(raised.exception.response, result)

    def test_provider_cannot_create_raw_directory_write_file_or_log_rejected_response(self):
        for code in (0, 2002):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                raw = self.encoded_payload({"code": code, "msg": self.credential, "data": None})
                client, response, _ = self.client_for(raw)
                root = Path(temporary)
                request_log = []
                provider = V2TushareProvider(raw_root=root / "inputs" / "raw", request_log=request_log, client=client)
                with mock.patch("swivd.v2_provider.atomic_write_bytes") as writer:
                    with self.assertRaisesRegex(TushareProtocolError, "^response contains credential material$"):
                        provider.trade_calendar("20211213", "20211213")
                writer.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])
                self.assertEqual(request_log, [])
                self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
