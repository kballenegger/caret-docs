"""Pairing tests: the QR must never be a durable credential.

Every test here is about one of two things — that a phone can pair without
anyone typing a 43-character key, and that the thing travelling as pixels
is worthless five minutes later. The second is the reason the first is
allowed to exist, so the failure paths are tested harder than the happy
one.

Hermetic: a real server on loopback, no network, no clock control beyond
minting tokens with an explicit TTL.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import adapters, pairing  # noqa: E402
from helpers import API_KEY, TestServer  # noqa: E402

SERVER_URL = "https://caret.example.ts.net"


class _PairingCase(unittest.TestCase):
    def server(self, **overrides) -> TestServer:
        server = TestServer(**overrides)
        self.addCleanup(server.close)
        return server

    def mint(self, server: TestServer, **body):
        payload = {"server_url": SERVER_URL}
        payload.update(body)
        return server.request("POST", "/v1/pairing/tokens", body=payload)

    def claim(self, server: TestServer, token: str, *, url: str = SERVER_URL):
        return server.request(
            "POST",
            "/v1/pairing/claim",
            body={"pairing_token": token, "server_url": url},
            key=None,
        )


class QrPayloadTests(_PairingCase):
    """What the QR actually carries."""

    def test_the_payload_never_contains_the_api_key(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        self.assertNotIn(API_KEY, minted["payload"])
        self.assertNotIn(API_KEY, str(minted))

    def test_the_payload_is_a_versioned_caret_pair_uri(self):
        _, minted, _ = self.mint(self.server())
        parsed = urlparse(minted["payload"])
        self.assertEqual(parsed.scheme, pairing.PAIRING_SCHEME)
        self.assertEqual(parsed.netloc, pairing.PAIRING_HOST)
        query = parse_qs(parsed.query)
        self.assertEqual(query["v"], [pairing.PAIRING_PAYLOAD_VERSION])
        self.assertEqual(unquote(query["u"][0]), SERVER_URL)
        self.assertEqual(query["t"], [minted["pairing_token"]])

    def test_the_payload_fits_a_scannable_qr(self):
        # A payload that needs a dense symbol is a payload nobody can scan
        # from across a desk. Guard the size, not just the syntax.
        _, minted, _ = self.mint(self.server())
        self.assertLess(len(minted["payload"]), 180)

    def test_the_mint_response_says_it_is_single_use_and_when_it_dies(self):
        _, minted, _ = self.mint(self.server(), ttl_seconds=120)
        self.assertTrue(minted["single_use"])
        self.assertEqual(minted["expires_in_seconds"], 120)
        self.assertTrue(minted["expires_at"].endswith("Z"))


class ClaimTests(_PairingCase):
    def test_a_claim_returns_the_key_and_the_capabilities(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        status, body, _ = self.claim(server, minted["pairing_token"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["api_key"], API_KEY)
        self.assertEqual(body["server_url"], SERVER_URL)
        self.assertEqual(body["token_id"], minted["token_id"])
        # The client should not have to make a second call to find out what
        # it just paired with.
        self.assertTrue(body["capabilities"]["dictation"])
        self.assertTrue(body["readiness"]["ready"])

    def test_claiming_is_anonymous_because_the_client_has_no_key_yet(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        status, _, _ = self.claim(server, minted["pairing_token"])
        self.assertEqual(status, 200)

    def test_a_second_claim_is_refused_as_consumed(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        self.claim(server, minted["pairing_token"])
        status, body, _ = self.claim(server, minted["pairing_token"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "pairing_token_consumed")

    def test_an_unknown_token_is_refused_without_hinting(self):
        status, body, _ = self.claim(self.server(), "not-a-real-token")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "pairing_token_invalid")

    def test_claiming_against_a_different_url_is_refused(self):
        # The attack this closes: relay the QR, claim it against a server
        # you control, and the key is delivered to you.
        server = self.server()
        _, minted, _ = self.mint(server)
        status, body, _ = self.claim(
            server, minted["pairing_token"], url="https://attacker.example"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "pairing_url_mismatch")

    def test_a_url_that_differs_only_in_case_or_trailing_slash_still_works(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        status, body, _ = self.claim(
            server, minted["pairing_token"], url="https://CARET.example.ts.net/"
        )
        self.assertEqual(status, 200, body)

    def test_a_claim_needs_both_fields(self):
        server = self.server()
        for body in ({"server_url": SERVER_URL}, {"pairing_token": "x"}):
            status, payload, _ = server.request(
                "POST", "/v1/pairing/claim", body=body, key=None
            )
            self.assertEqual(status, 400, payload)


class ExpiryTests(_PairingCase):
    def test_an_expired_token_is_refused_with_its_own_code(self):
        server = self.server()
        # Mint through the registry so the expiry can be set in the past
        # without sleeping through a real TTL.
        token, _ = server.backend.pairing.mint(
            server_url=SERVER_URL,
            api_key=API_KEY,
            ttl_seconds=pairing.MIN_TTL_SECONDS,
            now=time.time() - 3600,
        )
        status, body, _ = self.claim(server, token)
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "pairing_token_expired")

    def test_a_ttl_outside_the_window_is_refused(self):
        server = self.server()
        for ttl in (1, pairing.MAX_TTL_SECONDS + 1):
            status, body, _ = self.mint(server, ttl_seconds=ttl)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "bad_request")

    def test_a_non_integer_ttl_is_refused_rather_than_coerced(self):
        status, body, _ = self.mint(self.server(), ttl_seconds="300")
        self.assertEqual(status, 400, body)

    def test_the_default_ttl_is_minutes_not_hours(self):
        _, minted, _ = self.mint(self.server())
        self.assertEqual(minted["expires_in_seconds"], pairing.DEFAULT_TTL_SECONDS)
        self.assertLessEqual(pairing.DEFAULT_TTL_SECONDS, 900)


class RevocationTests(_PairingCase):
    def test_a_revoked_token_cannot_be_claimed(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        status, body, _ = server.request(
            "POST", "/v1/pairing/revoke", body={"token_id": minted["token_id"]}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], 1)
        status, body, _ = self.claim(server, minted["pairing_token"])
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "pairing_token_revoked")

    def test_revoke_all_clears_every_pending_token(self):
        server = self.server()
        tokens = [self.mint(server)[1] for _ in range(3)]
        status, body, _ = server.request(
            "POST", "/v1/pairing/revoke", body={"all": True}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], 3)
        for minted in tokens:
            status, _, _ = self.claim(server, minted["pairing_token"])
            self.assertEqual(status, 410)

    def test_revoking_an_unknown_token_is_a_404_not_a_silent_success(self):
        status, body, _ = self.server().request(
            "POST", "/v1/pairing/revoke", body={"token_id": "pt_deadbeef"}
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "pairing_token_invalid")

    def test_listing_shows_state_but_never_the_token_or_the_key(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        status, body, _ = server.request("GET", "/v1/pairing/tokens")
        self.assertEqual(status, 200, body)
        entry = body["tokens"][0]
        self.assertEqual(entry["token_id"], minted["token_id"])
        self.assertEqual(entry["state"], "pending")
        raw = str(body)
        self.assertNotIn(minted["pairing_token"], raw)
        self.assertNotIn(API_KEY, raw)

    def test_a_claimed_token_lists_as_claimed(self):
        server = self.server()
        _, minted, _ = self.mint(server)
        self.claim(server, minted["pairing_token"])
        _, body, _ = server.request("GET", "/v1/pairing/tokens")
        self.assertEqual(body["tokens"][0]["state"], "claimed")


class AuthTests(_PairingCase):
    def test_minting_requires_a_key(self):
        status, body, _ = self.server().request(
            "POST", "/v1/pairing/tokens", body={"server_url": SERVER_URL}, key=None
        )
        self.assertEqual(status, 401, body)

    def test_listing_and_revoking_require_a_key(self):
        server = self.server()
        for method, path, body in (
            ("GET", "/v1/pairing/tokens", None),
            ("POST", "/v1/pairing/revoke", {"all": True}),
        ):
            status, payload, _ = server.request(method, path, body=body, key=None)
            self.assertEqual(status, 401, payload)

    def test_pairing_hands_over_the_key_that_minted_it(self):
        # Not "an" API key — the one the operator authenticated with, so a
        # caller can never mint a device a credential they do not hold.
        server = self.server(api_keys=("first-key", "second-key"))
        _, minted, _ = server.request(
            "POST",
            "/v1/pairing/tokens",
            body={"server_url": SERVER_URL},
            key="second-key",
        )
        _, body, _ = self.claim(server, minted["pairing_token"])
        self.assertEqual(body["api_key"], "second-key")


class ReadinessTests(_PairingCase):
    def test_a_backend_without_stt_refuses_to_pair(self):
        # Pairing a phone to a backend that cannot take dictation would hand
        # the user a keyboard with the microphone missing and no explanation.
        server = self.server(transcriber=adapters.NullTranscriber())
        status, body, _ = self.mint(server)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "backend_not_ready")
        self.assertIn("no_stt_adapter", body["error"]["message"])

    def test_a_backend_without_an_agent_pairs_fine(self):
        # Ask is optional; dictation-only is a valid backend.
        server = self.server(agent=adapters.NullAgent())
        status, body, _ = self.mint(server)
        self.assertEqual(status, 200, body)
        _, claimed, _ = self.claim(server, body["pairing_token"])
        self.assertFalse(claimed["capabilities"]["draft"])
        self.assertTrue(claimed["capabilities"]["dictation"])


class TransportTests(_PairingCase):
    def test_a_plaintext_server_url_is_refused_except_on_loopback(self):
        server = self.server()
        status, body, _ = self.mint(server, server_url="http://caret.example.ts.net")
        self.assertEqual(status, 400, body)
        self.assertIn("https", body["error"]["message"])
        status, body, _ = self.mint(server, server_url="http://127.0.0.1:8787")
        self.assertEqual(status, 200, body)

    def test_a_server_url_is_required(self):
        status, body, _ = self.server().request(
            "POST", "/v1/pairing/tokens", body={}
        )
        self.assertEqual(status, 400, body)

    def test_pairing_can_be_turned_off_entirely(self):
        server = self.server(pairing_enabled=False)
        status, body, _ = self.mint(server)
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "pairing_disabled")
        _, health, _ = server.request("GET", "/v1/health", key=None)
        self.assertEqual(health["routes"]["pairing"], {"route": "off"})

    def test_health_advertises_that_the_qr_carries_no_key(self):
        _, health, _ = self.server().request("GET", "/v1/health", key=None)
        self.assertEqual(
            health["routes"]["pairing"],
            {"route": "token", "single_use": True, "carries_api_key": False},
        )


class RegistryTests(unittest.TestCase):
    """Unit-level rules that never reach HTTP."""

    def test_the_plaintext_token_is_never_stored(self):
        registry = pairing.PairingRegistry()
        token, record = registry.mint(server_url=SERVER_URL, api_key="k")
        self.assertNotIn(token, str(record))
        self.assertNotEqual(record.token_digest, token)

    def test_a_flood_of_mints_is_capped(self):
        registry = pairing.PairingRegistry(max_active=2)
        registry.mint(server_url=SERVER_URL, api_key="k")
        registry.mint(server_url=SERVER_URL, api_key="k")
        with self.assertRaises(Exception) as caught:
            registry.mint(server_url=SERVER_URL, api_key="k")
        self.assertEqual(caught.exception.status, 429)

    def test_dead_records_are_pruned_so_keys_do_not_linger(self):
        registry = pairing.PairingRegistry()
        past = time.time() - 3600
        registry.mint(server_url=SERVER_URL, api_key="k", now=past)
        self.assertEqual(registry.prune(), 1)
        self.assertEqual(registry.list(), [])

    def test_urls_normalise_to_one_canonical_form(self):
        forms = [
            "https://Caret.Example.ts.net",
            "https://caret.example.ts.net/",
            "https://caret.example.ts.net",
        ]
        canonical = {pairing.normalise_server_url(f) for f in forms}
        self.assertEqual(canonical, {"https://caret.example.ts.net"})

    def test_a_url_without_a_scheme_is_refused(self):
        with self.assertRaises(Exception):
            pairing.normalise_server_url("caret.example.ts.net")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
