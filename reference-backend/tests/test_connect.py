"""`caret-connect:v1:` payload tests — deterministic, byte for byte.

The Caret client pins this encoding in its own test suite. If the two
implementations ever disagree by one byte, the QR scans and then fails,
which is the hardest kind of setup bug to diagnose from a phone. So the
first test here is the worked example itself, pinned to the same bytes.

The key in every vector below is fake and exists only as a test vector.
Real keys never appear in this repository.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import connect  # noqa: E402
from caret_backend.errors import CaretError  # noqa: E402

DEMO_KEY = "caret_demo_key_1234567890"
DEMO_URL = "https://agent.example.com"
DEMO_PAYLOAD = (
    "caret-connect:v1:eyJrIjoiY2FyZXRfZGVtb19rZXlfMTIzNDU2Nzg5MCIsInQiOiJhZ2Vu"
    "dCIsInUiOiJodHRwczovL2FnZW50LmV4YW1wbGUuY29tIn0"
)


class WorkedExampleTests(unittest.TestCase):
    def test_the_pinned_example_encodes_to_the_pinned_bytes(self):
        self.assertEqual(connect.encode_payload(DEMO_URL, DEMO_KEY), DEMO_PAYLOAD)

    def test_the_pinned_example_decodes_back(self):
        self.assertEqual(
            connect.decode_payload(DEMO_PAYLOAD),
            {"k": DEMO_KEY, "t": "agent", "u": DEMO_URL},
        )

    def test_the_encoding_matches_the_documented_shell_pipeline(self):
        # base64 → +/ to -_ → strip padding, over minified sorted JSON.
        document = json.dumps(
            {"k": DEMO_KEY, "t": "agent", "u": DEMO_URL},
            separators=(",", ":"),
            sort_keys=True,
        )
        expected = (
            base64.b64encode(document.encode())
            .decode()
            .replace("+", "-")
            .replace("/", "_")
            .rstrip("=")
        )
        self.assertEqual(DEMO_PAYLOAD, connect.CONNECT_PREFIX + expected)

    def test_encoding_is_deterministic_across_calls(self):
        self.assertEqual(
            connect.encode_payload(DEMO_URL, DEMO_KEY),
            connect.encode_payload(DEMO_URL, DEMO_KEY),
        )


class FormatTests(unittest.TestCase):
    def payload_json(self, url: str = DEMO_URL, key: str = DEMO_KEY) -> str:
        body = connect.encode_payload(url, key)[len(connect.CONNECT_PREFIX) :]
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()

    def test_the_json_is_minified_with_sorted_keys(self):
        self.assertEqual(
            self.payload_json(),
            '{"k":"caret_demo_key_1234567890","t":"agent","u":"https://agent.example.com"}',
        )

    def test_the_base64_is_unpadded_urlsafe(self):
        body = connect.encode_payload(DEMO_URL, DEMO_KEY)[len(connect.CONNECT_PREFIX) :]
        self.assertNotIn("=", body)
        self.assertNotIn("+", body)
        self.assertNotIn("/", body)

    def test_this_backend_always_declares_itself_an_agent(self):
        self.assertEqual(json.loads(self.payload_json())["t"], "agent")


class UrlTests(unittest.TestCase):
    def test_https_is_required_off_the_local_network(self):
        with self.assertRaises(CaretError) as caught:
            connect.encode_payload("http://agent.example.com", DEMO_KEY)
        self.assertIn("https", caught.exception.message)

    def test_plaintext_is_allowed_exactly_where_the_client_allows_it(self):
        for url in (
            "http://localhost:8787",
            "http://127.0.0.1:8787",
            "http://192.168.1.20:8787",
            "http://10.0.0.4:8787",
            "http://172.16.3.9:8787",
            "http://my-mac.tail1234.ts.net",
        ):
            with self.subTest(url=url):
                self.assertTrue(connect.encode_payload(url, DEMO_KEY))

    def test_urls_are_canonicalised(self):
        self.assertEqual(
            connect.normalise_base_url("https://Agent.Example.com/"),
            "https://agent.example.com",
        )

    def test_a_url_without_a_scheme_is_refused(self):
        with self.assertRaises(CaretError):
            connect.encode_payload("agent.example.com", DEMO_KEY)


class KeyTests(unittest.TestCase):
    def test_a_key_outside_the_clients_length_range_is_refused_here(self):
        # Better a sentence in the terminal than a silent rejection on the
        # phone with nothing to explain it.
        for key in ("short", "k" * 301):
            with self.subTest(length=len(key)), self.assertRaises(CaretError):
                connect.encode_payload(DEMO_URL, key)

    def test_an_empty_key_is_refused(self):
        with self.assertRaises(CaretError):
            connect.encode_payload(DEMO_URL, "  ")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(
            connect.encode_payload(DEMO_URL, f"  {DEMO_KEY}\n"), DEMO_PAYLOAD
        )


class HostedTests(unittest.TestCase):
    """This backend never issues hosted codes, but decoding is a reference
    implementation others check against, so it has to get them right."""

    def encode(self, document: dict) -> str:
        raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        return connect.CONNECT_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def test_a_hosted_payload_without_a_url_decodes(self):
        payload = self.encode({"k": DEMO_KEY, "t": "hosted"})
        self.assertEqual(connect.decode_payload(payload)["t"], "hosted")

    def test_a_hosted_payload_that_names_a_url_is_refused(self):
        # Anti-redirect: the client pins its own canonical URL, so a hosted
        # code carrying one is an attempt to aim a phone elsewhere.
        payload = self.encode({"k": DEMO_KEY, "t": "hosted", "u": "https://evil.example"})
        with self.assertRaises(CaretError) as caught:
            connect.decode_payload(payload)
        self.assertIn("omit u", caught.exception.message)

    def test_an_agent_payload_without_a_url_is_refused(self):
        payload = self.encode({"k": DEMO_KEY, "t": "agent"})
        with self.assertRaises(CaretError):
            connect.decode_payload(payload)

    def test_unknown_fields_are_tolerated(self):
        payload = self.encode(
            {"k": DEMO_KEY, "t": "agent", "u": DEMO_URL, "future": "field"}
        )
        self.assertEqual(connect.decode_payload(payload)["future"], "field")

    def test_an_unknown_type_is_refused(self):
        payload = self.encode({"k": DEMO_KEY, "t": "spaceship", "u": DEMO_URL})
        with self.assertRaises(CaretError):
            connect.decode_payload(payload)


class MalformedTests(unittest.TestCase):
    def test_a_payload_without_the_prefix_is_refused(self):
        with self.assertRaises(CaretError) as caught:
            connect.decode_payload("https://agent.example.com")
        self.assertIn("caret-connect:v1:", caught.exception.message)

    def test_a_v2_payload_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(CaretError):
            connect.decode_payload("caret-connect:v2:abcd")

    def test_undecodable_base64_is_refused(self):
        with self.assertRaises(CaretError):
            connect.decode_payload(connect.CONNECT_PREFIX + "!!!!not base64!!!!")

    def test_a_json_array_is_refused(self):
        body = base64.urlsafe_b64encode(b"[1,2,3]").decode().rstrip("=")
        with self.assertRaises(CaretError):
            connect.decode_payload(connect.CONNECT_PREFIX + body)


class MaskingTests(unittest.TestCase):
    """The payload is the API key in another encoding. What may appear in
    console output, a log line, or a bug report is `mask()` — and only it."""

    def test_the_mask_never_contains_the_key(self):
        masked = connect.mask(DEMO_PAYLOAD)
        self.assertNotIn(DEMO_KEY, masked)
        # ...nor enough of the payload to rebuild it.
        self.assertNotIn(DEMO_PAYLOAD[len(connect.CONNECT_PREFIX) :], masked)

    def test_the_mask_says_it_is_redacted_and_how_long(self):
        masked = connect.mask(DEMO_PAYLOAD)
        self.assertIn("redacted", masked)
        self.assertIn("103", masked)

    def test_two_different_payloads_mask_differently(self):
        other = connect.encode_payload("https://other.example.com", DEMO_KEY)
        self.assertNotEqual(connect.mask(DEMO_PAYLOAD), connect.mask(other))

    def test_the_mask_leaks_no_run_of_the_payload_itself(self):
        body = DEMO_PAYLOAD[len(connect.CONNECT_PREFIX) :]
        masked = connect.mask(DEMO_PAYLOAD)
        for start in range(0, len(body) - 4):
            self.assertNotIn(body[start : start + 4], masked)

    def test_an_empty_input_does_not_fall_through(self):
        self.assertNotIn("None", connect.mask(""))
        self.assertTrue(connect.mask("").startswith(connect.CONNECT_PREFIX))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
