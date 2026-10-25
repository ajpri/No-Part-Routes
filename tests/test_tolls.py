import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import Mock, patch

import api_documentation as docs
import no_part_routes as npr
import test_service as fixtures


def response(tolls=False, duration="60s", distance=100):
    route = {"duration": duration, "distanceMeters": distance}
    if tolls:
        route["travelAdvisory"] = {"tollInfo": {}}
    return io.BytesIO(json.dumps({"routes": [route]}).encode())


class GoogleTollTests(unittest.TestCase):
    def test_known_price_detected_without_returning_price(self):
        payload = {"routes": [{"travelAdvisory": {"tollInfo": {
            "estimatedPrice": [{"currencyCode": "USD", "units": "5"}]}}}]}
        client = npr.GoogleClient("fake", Mock(return_value=io.BytesIO(json.dumps(payload).encode())))
        result = client.route({}, {})
        self.assertTrue(result["usesTolls"])
        self.assertEqual(set(result), {"driveMinutes", "distanceMiles", "summary", "usesTolls"})

    def test_malformed_toll_metadata_does_not_silently_report_no_tolls(self):
        for advisory in (None, [], "bad", {"tollInfo": None}, {"tollInfo": []}):
            with self.subTest(advisory=advisory):
                payload = {"routes": [{"travelAdvisory": advisory}]}
                client = npr.GoogleClient("fake", Mock(return_value=io.BytesIO(json.dumps(payload).encode())))
                with self.assertRaises(npr.APIError) as error:
                    client.route({}, {})
                self.assertEqual(error.exception.code, "GOOGLE_INVALID_RESPONSE")


class TollHTTPTests(fixtures.HTTPFixture):
    def client(self, replies, limit=10):
        self.opener = Mock(side_effect=replies)
        self.guard = npr.UsageGuard(self.directory / "usage.ini", limit, 10)
        self.app.google = npr.GoogleClient("fake-key", self.opener, usage=self.guard)

    def test_no_tolls_uses_one_call_and_null_alternative(self):
        self.client([response()])
        status, _, raw = self.post()
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(result["usesTolls"])
        self.assertIsNone(result["avoidTolls"])
        self.assertIsNone(result["avoidTollsError"])
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(self.guard.counts["routes"], 1)

    def test_tolls_with_unknown_price_trigger_second_call_and_reuse_geocoding(self):
        geocode = io.BytesIO(b'{"status":"OK","results":[{"place_id":"home-id"}]}')
        self.client([geocode, response(tolls=True), response(duration="120s", distance=200)])
        status, _, raw = self.post({"from": {"label": "home"}, "to": fixtures.POINT})
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(result["usesTolls"])
        self.assertEqual(result["driveMinutes"], 1.0)
        self.assertEqual(result["avoidTolls"], {
            "driveMinutes": 2.0, "distanceMiles": 0.12, "summary": None, "usesTolls": False})
        self.assertIsNone(result["avoidTollsError"])
        payloads = [json.loads(call.args[0].data) for call in self.opener.call_args_list[1:]]
        for index, payload in enumerate(payloads):
            self.assertEqual(payload["routeModifiers"], {"avoidTolls": bool(index)})
            self.assertEqual(payload["extraComputations"], ["TOLLS"])
            self.assertEqual(payload["routingPreference"], "TRAFFIC_AWARE")
            self.assertNotIn("departureTime", payload)
            self.assertIn("routes.travelAdvisory.tollInfo", self.opener.call_args_list[index + 1].args[0].get_header("X-goog-fieldmask"))
        self.assertEqual(payloads[0]["origin"], payloads[1]["origin"])
        self.assertEqual(payloads[0]["destination"], payloads[1]["destination"])
        self.assertEqual(self.guard.counts, {"routes": 2, "geocoding": 1})

    def test_avoid_tolls_may_still_use_tolls_and_never_triggers_third_call(self):
        self.client([response(tolls=True), response(tolls=True)])
        _, _, raw = self.post()
        result = json.loads(raw)
        self.assertTrue(result["avoidTolls"]["usesTolls"])
        self.assertIsNone(result["avoidTollsError"])
        self.assertEqual(self.opener.call_count, 2)

    def test_second_call_limit_retains_primary_without_sending_blocked_call(self):
        self.client([response(tolls=True)], limit=1)
        status, _, raw = self.post()
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(result["usesTolls"])
        self.assertIsNone(result["avoidTolls"])
        self.assertEqual(result["avoidTollsError"]["code"], "DAILY_USAGE_LIMIT")
        self.assertEqual(self.guard.counts["routes"], 1)
        self.assertEqual(self.opener.call_count, 1)

    def test_second_call_errors_retained_as_partial_success(self):
        cases = [(socket.timeout(), "GOOGLE_TIMEOUT"),
                 (urllib.error.HTTPError("private-url", 403, "secret", {}, io.BytesIO(b'private')), "GOOGLE_REJECTED"),
                 (io.BytesIO(b'{"routes":[]}'), "NO_ROUTE"),
                 (io.BytesIO(b'{"routes":[{}],"fallbackInfo":{"routingMode":"TRAFFIC_UNAWARE"}}'), "TRAFFIC_UNAVAILABLE"),
                 (io.BytesIO(b'{"routes":[{"travelAdvisory":{"tollInfo":null}}]}'), "GOOGLE_INVALID_RESPONSE")]
        for reply, code in cases:
            with self.subTest(code=code):
                # Fresh guard file for each case so limits do not interfere.
                (self.directory / "usage.ini").unlink(missing_ok=True)
                self.client([response(tolls=True), reply])
                status, _, raw = self.post()
                result = json.loads(raw)
                self.assertEqual(status, 200)
                self.assertEqual(result["driveMinutes"], 1.0)
                self.assertIsNone(result["avoidTolls"])
                self.assertEqual(result["avoidTollsError"]["code"], code)
                self.assertNotIn(b"private", raw)
                self.assertEqual(self.guard.counts["routes"], 2)

    def test_primary_failure_is_http_error_without_second_call(self):
        self.client([socket.timeout()])
        status, _, raw = self.post()
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(raw)["error"]["code"], "GOOGLE_TIMEOUT")
        self.assertEqual(self.opener.call_count, 1)

    def test_second_usage_storage_failure_preserves_primary(self):
        self.client([response(tolls=True)])
        save = self.guard._save

        def fail_second_save(day, counts):
            if counts["routes"] == 2:
                raise OSError("private-path")
            save(day, counts)

        with patch.object(self.guard, "_save", side_effect=fail_second_save):
            status, _, raw = self.post()
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(result["avoidTollsError"]["code"], "USAGE_STORAGE_UNAVAILABLE")
        self.assertNotIn(b"private-path", raw)
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(self.guard.counts["routes"], 1)

    def test_second_invalid_place_id_still_invalidates_cache(self):
        primary = {**self.google.route.return_value, "usesTolls": True}
        self.google.route.side_effect = [primary, npr.APIError(502, "GOOGLE_REJECTED", "Google could not complete the request.", ["origin"])]
        status, _, raw = self.post({"from": {"label": "home"}, "to": fixtures.POINT})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["avoidTollsError"]["code"], "GOOGLE_REJECTED")
        self.assertEqual(self.app.cache.rows, {})
        self.google.geocode.assert_called_once_with("10 Example St")

    def test_openapi_describes_partial_success_and_current_versions(self):
        contract = docs.openapi()
        schema = contract["paths"]["/route"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(set(schema["required"]), {
            "driveMinutes", "distanceMiles", "summary", "usesTolls", "avoidTolls", "avoidTollsError"})
        self.assertEqual(schema["properties"]["avoidTolls"]["oneOf"][0]["required"], [
            "driveMinutes", "distanceMiles", "summary", "usesTolls"])
        self.assertEqual(docs.API_VERSION, "26.10.25")
        self.assertEqual(docs.DOCS_VERSION, 10)
