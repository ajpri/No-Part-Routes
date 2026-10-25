import concurrent.futures
from datetime import datetime, timedelta, timezone
import csv
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

import api_documentation as docs
import no_part_routes as npr

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
POINT = {"coordinates": {"latitude": 29.76, "longitude": -95.37}}
BODY = {"from": POINT, "to": POINT}


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def config_files(self, places="", keys="[Muse]\nsecret = 5678\nnote = reminder\n", settings=None):
        (self.directory / "settings.ini").write_text(settings or "[server]\nhost = 127.0.0.1\nport = 8080\n[google]\napi_key = fake-google-key\n", encoding="utf-8")
        (self.directory / "apiKeys.ini").write_text(keys, encoding="utf-8")
        (self.directory / "locations.ini").write_text(places, encoding="utf-8")
        return npr.Config.load(self.directory)


class ConfigTests(TemporaryTest):
    def test_public_sample_secrets_are_rejected_without_echoing_values(self):
        samples = npr.read_ini(npr.BASE_DIR / "apiKeys.example.ini")
        secrets = [samples.get(section, "secret") for section in samples.sections()]
        secrets.extend(["1234", "my-grok-secret", "a-longer-secret-if-i-want",
                        "CHANGE-ME-another-public-placeholder"])
        for secret in secrets:
            with self.subTest(secret=secret):
                with self.assertRaises(npr.ConfigError) as result:
                    self.config_files(keys=f"[valid]\nsecret=unique-client-secret\n[unused]\nsecret=  {secret}  \n")
                self.assertIn("Replace every sample secret", str(result.exception))
                self.assertNotIn(secret, str(result.exception))

    def test_multiple_simple_secrets_and_literal_percent(self):
        config = self.config_files(keys="[Muse]\nsecret = 5678\nnote = a note\n[Grok]\nsecret = s%20 ; internal text\n")
        self.assertEqual(config.secrets, ("5678", "s%20 ; internal text"))

    def test_labels_casefold_and_coordinates(self):
        config = self.config_files("[Home]\naddress =  10 Example St\n[work]\nlatitude=29.76\nlongitude=-95.37\n")
        self.assertEqual(config.labels["home"], {"address": "10 Example St"})
        self.assertEqual(config.labels["work"], POINT)

    def test_pluscode_labels_full_and_compound(self):
        config = self.config_files("[meeting]\npluscode = 849VCWC8+R9\n[local]\npluscode = CWC8+R9 Mountain View, CA, USA\n")
        self.assertEqual(config.labels["meeting"], {"address": "849VCWC8+R9"})
        self.assertEqual(config.labels["local"], {"address": "CWC8+R9 Mountain View, CA, USA"})

    def test_pluscode_config_empty_or_conflicting_rejected(self):
        for places in ("[meeting]\npluscode=", "[meeting]\npluscode=849VCWC8+R9\naddress=10 Example St",
                       "[meeting]\npluscode=849VCWC8+R9\nlatitude=1\nlongitude=2"):
            with self.subTest(places=places), self.assertRaises(npr.ConfigError):
                self.config_files(places)

    def test_bad_locations_fail_without_exposing_value(self):
        for places in ("[home]\naddress=private location\nlatitude=1\nlongitude=2", "[home]\nlatitude=91\nlongitude=2", "[Home]\naddress=private location\n[home]\naddress=other", "[home]\naddress=", "[home]\nlatitude=nan\nlongitude=1"):
            with self.subTest(places=places), self.assertRaises(npr.ConfigError) as result:
                self.config_files(places)
            self.assertNotIn("private location", str(result.exception))

    def test_missing_files_and_invalid_ini_are_sanitized(self):
        with self.assertRaises(npr.ConfigError):
            npr.Config.load(self.directory)
        (self.directory / "settings.ini").write_text("secret-in-a-bad-line")
        with self.assertRaises(npr.ConfigError) as result:
            npr.Config.load(self.directory)
        self.assertNotIn("secret-in-a-bad-line", str(result.exception))

    def test_keys_cannot_be_empty_or_multiline(self):
        for keys in ("", "[Muse]\nsecret=", "[Muse]\nsecret=abc\n  def"):
            with self.subTest(keys=keys), self.assertRaises(npr.ConfigError):
                self.config_files(keys=keys)

    def test_default_sections_rejected(self):
        with self.assertRaises(npr.ConfigError):
            self.config_files(keys="[DEFAULT]\nsecret=5678\n[Muse]\nnote=reminder")

    def test_placeholder_and_port_validation(self):
        for port, key in ((0, "key"), (65536, "key"), (8080, "paste-your-google-api-key-here")):
            with self.subTest(port=port, key=key), self.assertRaises(npr.ConfigError):
                self.config_files(settings=f"[server]\nhost=127.0.0.1\nport={port}\n[google]\napi_key={key}")


class CacheTests(TemporaryTest):
    def cache(self, now=lambda: NOW):
        return npr.LocationCache(self.directory / "locationCache.csv", now=now)

    def seed_coordinates(self, cache, expiry):
        cache.resolve("10 Example St", lambda address: "place-home")
        cache.rows["10 example st"].update(latitude="29.76", longitude="-95.37", coordinates_expires_at=npr.stamp(expiry))
        cache._save()

    def test_missing_file_and_shared_normalized_cache(self):
        cache = self.cache()
        geocode = Mock(return_value="id")
        self.assertTrue(cache.path.exists())
        self.assertEqual(cache.resolve(" 10   Example St ", geocode), "id")
        self.assertEqual(cache.resolve("10 example st", geocode), "id")
        geocode.assert_called_once()
        cache.resolve("10 Example St, Unit 2", geocode)
        self.assertEqual(geocode.call_count, 2)

    def test_csv_roundtrip_quotes_commas_and_unicode(self):
        cache = self.cache()
        address = '10 "Example", Montréal\nUnit 2'
        cache.resolve(address, lambda address: "place-id")
        reload = self.cache()
        self.assertEqual(reload.resolve(address, Mock(side_effect=AssertionError)), "place-id")
        self.assertEqual(reload.rows[npr.address_key(address)]["source_address"], address)

    def test_new_entries_never_store_google_coordinates(self):
        cache = self.cache()
        cache.resolve("10 Example St", lambda address: "id")
        row = cache.rows["10 example st"]
        self.assertEqual([row[key] for key in ("latitude", "longitude", "coordinates_expires_at")], ["", "", ""])

    def test_expiry_removes_coordinates_but_retains_place_id(self):
        cache = self.cache()
        self.seed_coordinates(cache, NOW + timedelta(days=30))
        before = cache.rows["10 example st"]["coordinates_expires_at"]
        cache.resolve("10 example st", Mock(side_effect=AssertionError))
        self.assertEqual(cache.rows["10 example st"]["coordinates_expires_at"], before)
        cache.now = lambda: NOW + timedelta(days=30)
        cache.cleanup()
        reloaded = self.cache(now=cache.now)
        self.assertEqual(reloaded.rows["10 example st"]["latitude"], "")
        self.assertEqual(reloaded.resolve("10 example st", Mock(side_effect=AssertionError)), "place-home")

    def test_startup_cleans_expired_coordinates(self):
        cache = self.cache()
        self.seed_coordinates(cache, NOW + timedelta(days=1))
        cache = self.cache(now=lambda: NOW + timedelta(days=2))
        self.assertEqual(cache.rows["10 example st"]["longitude"], "")
        self.assertNotIn("29.76", cache.path.read_text())

    def test_cleanup_retries_failed_persistence(self):
        cache = self.cache()
        self.seed_coordinates(cache, NOW + timedelta(days=1))
        cache.now = lambda: NOW + timedelta(days=2)
        with patch.object(cache, "_save", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                cache.cleanup()
        self.assertTrue(cache.cleanup_pending)
        cache.cleanup()
        self.assertFalse(cache.cleanup_pending)
        self.assertNotIn("29.76", cache.path.read_text())

    def test_invalid_cache_recovery_never_archives_coordinates(self):
        cache = self.cache()
        self.seed_coordinates(cache, NOW + timedelta(days=1))
        with cache.path.open("a", encoding="utf-8") as stream:
            stream.write("damaged,row\n")
        with self.assertLogs(npr.LOG, level="WARNING"):
            recovered = self.cache()
        self.assertEqual(recovered.rows, {})
        backup = recovered.path.with_name(recovered.path.name + ".damaged.csv").read_text()
        self.assertIn("place-home", backup)
        self.assertNotIn("29.76", backup)
        self.assertNotIn("-95.37", backup)

    def test_corrupt_header_and_leftover_temporary_files(self):
        path = self.directory / "locationCache.csv"
        path.write_text("garbage\n")
        temporary = path.with_name(path.name + ".abc.tmp")
        temporary.write_text("private coordinate content")
        with self.assertLogs(npr.LOG, level="WARNING"):
            cache = self.cache()
        self.assertEqual(cache.rows, {})
        self.assertFalse(temporary.exists())

    def test_future_expiry_beyond_30_days_rejected(self):
        cache = self.cache()
        self.seed_coordinates(cache, NOW + timedelta(days=31))
        with self.assertLogs(npr.LOG, level="WARNING"):
            self.assertEqual(self.cache().rows, {})

    def test_concurrent_resolution_single_google_call(self):
        cache = self.cache()
        started, release = threading.Event(), threading.Event()
        def resolve(address):
            started.set()
            self.assertTrue(release.wait(3))
            return "one-id"
        geocode = Mock(side_effect=resolve)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(cache.resolve, "10 Example St", geocode) for _ in range(8)]
            self.assertTrue(started.wait(3))
            release.set()
            self.assertEqual([future.result(3) for future in futures], ["one-id"] * 8)
        geocode.assert_called_once()
        self.assertEqual(len(self.cache().rows), 1)

    def test_concurrent_different_addresses_preserve_all_rows(self):
        cache = self.cache()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: cache.resolve(f"{index} Example St", lambda address: "id-" + address), range(20)))
        self.assertEqual(len(self.cache().rows), 20)

    def test_cache_write_failure_does_not_discard_resolution(self):
        cache = self.cache()
        geocode = Mock(return_value="id")
        with patch.object(cache, "_save", side_effect=PermissionError), self.assertLogs(npr.LOG, level="WARNING"):
            self.assertEqual(cache.resolve("10 Example St", geocode), "id")
        self.assertEqual(cache.resolve("10 Example St", geocode), "id")
        geocode.assert_called_once()

    def test_geocoding_failure_not_cached(self):
        cache = self.cache()
        with self.assertRaises(npr.APIError):
            cache.resolve("10 Example St", Mock(side_effect=npr.APIError(503, "UNAVAILABLE", "failed")))
        self.assertEqual(cache.rows, {})
        self.assertEqual(cache.resolve("10 Example St", lambda address: "new-id"), "new-id")

    def test_invalidation_matches_place_id(self):
        cache = self.cache()
        cache.resolve("10 Example St", lambda address: "old-id")
        cache.invalidate("10 Example St", "different-id")
        self.assertEqual(len(cache.rows), 1)
        cache.invalidate("10 Example St", "old-id")
        self.assertEqual(self.cache().rows, {})


class GoogleTests(unittest.TestCase):
    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode())

    def test_geocode_only_returns_place_id_and_encodes_input(self):
        opener = Mock(return_value=self.response({"status": "OK", "results": [{"place_id": "id", "formatted_address": "private", "geometry": {"location": {"lat": 1, "lng": 2}}}]}))
        client = npr.GoogleClient("fake%key", opener)
        self.assertEqual(client.geocode("10 Example St & Unit 1"), "id")
        request = opener.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query["address"], ["10 Example St & Unit 1"])
        self.assertEqual(query["key"], ["fake%key"])
        self.assertEqual(opener.call_args.kwargs["timeout"], 15)

    def test_pluscode_plus_sign_is_encoded_once(self):
        for code in ("849VCWC8+R9", "CWC8+R9 Mountain View, CA, USA"):
            with self.subTest(code=code):
                opener = Mock(return_value=self.response({"status": "OK", "results": [{"place_id": "id"}]}))
                npr.GoogleClient("key", opener).geocode(code)
                url = opener.call_args.args[0].full_url
                self.assertIn("%2B", url)
                self.assertNotIn("%252B", url)
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["address"], [code])

    def test_geocoding_statuses(self):
        for status, expected in (("ZERO_RESULTS", 422), ("OVER_QUERY_LIMIT", 503), ("OVER_DAILY_LIMIT", 503), ("UNKNOWN_ERROR", 503), ("REQUEST_DENIED", 502), ("INVALID_REQUEST", 502), ("unexpected", 502)):
            with self.subTest(status=status):
                client = npr.GoogleClient("key", Mock(return_value=self.response({"status": status, "error_message": "secret"})))
                with self.assertRaises(npr.APIError) as result:
                    client.geocode("address")
                self.assertEqual(result.exception.status, expected)
                self.assertNotIn("secret", str(result.exception))

    def test_malformed_geocode_results(self):
        for results in ([], [{}], [{"place_id": ""}], [{"place_id": 5}], None):
            with self.subTest(results=results), self.assertRaises(npr.APIError) as result:
                npr.GoogleClient("key", Mock(return_value=self.response({"status": "OK", "results": results}))).geocode("address")
            self.assertEqual(result.exception.status, 502)

    def test_traffic_request_and_conversions(self):
        opener = Mock(return_value=self.response({"routes": [{"duration": "1104.5s", "distanceMeters": 11684, "description": "via Example Road"}]}))
        result = npr.GoogleClient("key", opener).route({"placeId": "one"}, {"placeId": "two"})
        self.assertEqual(result, {"driveMinutes": 18.4, "distanceMiles": 7.26, "summary": "via Example Road", "usesTolls": False})
        request = opener.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["routingPreference"], "TRAFFIC_AWARE")
        self.assertEqual(body["travelMode"], "DRIVE")
        self.assertNotIn("departureTime", body)
        self.assertFalse(body["computeAlternativeRoutes"])
        self.assertIn("fallbackInfo", request.get_header("X-goog-fieldmask"))
        self.assertEqual(opener.call_count, 1)

    def test_zero_proto_values_and_missing_summary(self):
        result = npr.GoogleClient("key", Mock(return_value=self.response({"routes": [{}]}))).route({}, {})
        self.assertEqual(result, {"driveMinutes": 0.0, "distanceMiles": 0.0, "summary": None, "usesTolls": False})

    def test_no_route_and_traffic_fallback(self):
        for payload, status in (({"routes": []}, 422), ({"routes": [{}], "fallbackInfo": {"routingMode": "TRAFFIC_UNAWARE"}}, 503)):
            with self.subTest(payload=payload), self.assertRaises(npr.APIError) as result:
                npr.GoogleClient("key", Mock(return_value=self.response(payload))).route({}, {})
            self.assertEqual(result.exception.status, status)

    def test_invalid_route_fields(self):
        for route in ({"duration": "NaNs"}, {"duration": "-1s"}, {"distanceMeters": -1}, {"distanceMeters": True}, {"description": ["bad"]}):
            with self.subTest(route=route), self.assertRaises(npr.APIError) as result:
                npr.GoogleClient("key", Mock(return_value=self.response({"routes": [route]}))).route({}, {})
            self.assertEqual(result.exception.status, 502)

    def test_http_error_timeout_and_invalid_json_mapping_no_retries(self):
        for error, status in ((urllib.error.HTTPError("url-with-key", 403, "key", {}, io.BytesIO(b'private upstream data')), 502),
                              (urllib.error.HTTPError("url", 429, "quota", {}, io.BytesIO()), 503),
                              (urllib.error.HTTPError("url", 500, "failed", {}, io.BytesIO()), 503),
                              (socket.timeout(), 504), (urllib.error.URLError(socket.timeout()), 504),
                              (urllib.error.URLError("secret"), 503)):
            with self.subTest(status=status):
                opener = Mock(side_effect=error)
                with self.assertRaises(npr.APIError) as result:
                    npr.GoogleClient("key", opener).geocode("address")
                self.assertEqual(result.exception.status, status)
                self.assertNotIn("secret", str(result.exception))
                self.assertEqual(opener.call_count, 1)
        with self.assertRaises(npr.APIError) as result:
            npr.GoogleClient("key", Mock(return_value=io.BytesIO(b'not json'))).geocode("address")
        self.assertEqual(result.exception.status, 502)

    def test_explicit_invalid_place_id_field_is_preserved_privately(self):
        payload = {"error": {"details": [{"fieldViolations": [{"field": "origin.place_id", "description": "Invalid place ID"}]}]}}
        opener = Mock(side_effect=urllib.error.HTTPError("url", 400, "bad", {}, self.response(payload)))
        with self.assertRaises(npr.APIError) as result:
            npr.GoogleClient("key", opener).route({}, {})
        self.assertEqual(result.exception.invalid_fields, ("origin",))


class ApplicationFixture(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.google = Mock()
        self.google.geocode.return_value = "place-id"
        self.google.route.return_value = {"driveMinutes": 1.2, "distanceMiles": 0.3, "summary": None, "usesTolls": False}
        config = npr.Config("127.0.0.1", 8080, "fake-google-key", ("5678", "ABC", "secret with spaces"),
                            {"home": {"address": "10 Example St"}, "work": POINT})
        self.app = npr.Application(config, npr.LocationCache(self.directory / "locationCache.csv"), self.google)


class ApplicationTests(ApplicationFixture):
    def test_pluscode_label_warming_direct_requests_and_cache_reload(self):
        code = "849VCWC8+R9"
        self.app.config.labels = {"meeting": npr.location({"pluscode": code}, {})}
        self.app.warm(threading.Event())
        for origin in ({"label": "MEETING"}, {"pluscode": " " + code + " "}, {"address": code.lower()}):
            self.app.route({"from": origin, "to": POINT})
        self.google.geocode.assert_called_once_with(code)
        self.assertEqual(self.google.route.call_count, 3)
        self.app.cache = npr.LocationCache(self.directory / "locationCache.csv")
        self.app.route({"from": {"pluscode": code}, "to": POINT})
        self.google.geocode.assert_called_once()
        self.assertEqual(self.google.route.call_args.args[0], {"placeId": "place-id"})

    def test_compound_pluscode_keeps_locality_and_rejects_invalid_representation(self):
        code = "CWC8+R9 Mountain View, CA, USA"
        self.app.route({"from": {"pluscode": code}, "to": POINT})
        self.google.geocode.assert_called_once_with(code)
        self.google.reset_mock()
        for endpoint in ({"pluscode": ""}, {"pluscode": 123}, {"pluscode": code, "address": code}):
            with self.subTest(endpoint=endpoint), self.assertRaises(npr.APIError) as result:
                self.app.route({"from": endpoint, "to": POINT})
            self.assertEqual(result.exception.status, 400)
        self.google.geocode.assert_not_called()

    def test_multiple_secrets_case_sensitive_and_no_complexity_rule(self):
        for secret in self.app.config.secrets:
            self.assertTrue(self.app.authorized("Bearer " + secret))
        for header in (None, "5678", "Bearer abc", "Bearer 5678 ", "Basic 5678"):
            self.assertFalse(self.app.authorized(header))

    def test_label_and_direct_address_share_cache_and_routes_are_always_fresh(self):
        self.app.route({"from": {"label": " HOME "}, "to": {"label": "work"}})
        self.app.route({"from": {"address": "10 EXAMPLE St"}, "to": POINT})
        self.google.geocode.assert_called_once_with("10 Example St")
        self.assertEqual(self.google.route.call_count, 2)
        self.assertEqual(self.google.route.call_args.args[0], {"placeId": "place-id"})
        self.assertEqual(self.google.route.call_args.args[1], {"location": {"latLng": POINT["coordinates"]}})

    def test_validate_both_endpoints_before_geocoding(self):
        for endpoint in ({"label": "unknown"}, {"coordinates": {"latitude": 91, "longitude": 0}}, {"address": ""}):
            with self.subTest(endpoint=endpoint), self.assertRaises(npr.APIError):
                self.app.route({"from": {"address": "10 Example St"}, "to": endpoint})
        self.google.geocode.assert_not_called()
        self.google.route.assert_not_called()

    def test_bad_inputs(self):
        cases = [None, [], {}, {**BODY, "departureTime": "now"},
                 {"from": {"label": "home", "address": "addr"}, "to": POINT},
                 {"from": {"coordinates": {"latitude": True, "longitude": 0}}, "to": POINT},
                 {"from": {"coordinates": {"latitude": 10**400, "longitude": 0}}, "to": POINT},
                 {"from": {"coordinates": {"latitude": float("nan"), "longitude": 0}}, "to": POINT},
                 {"from": {"coordinates": {"latitude": 0, "longitude": 0, "extra": 1}}, "to": POINT}]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(npr.APIError) as result:
                self.app.route(body)
            self.assertEqual(result.exception.status, 400)
        self.google.geocode.assert_not_called()

    def test_warming_failures_do_not_stop_other_labels(self):
        self.app.config.labels["other"] = {"address": "20 Example St"}
        self.google.geocode.side_effect = [npr.APIError(503, "UNAVAILABLE", "failed"), "other-id"]
        with self.assertLogs(npr.LOG, level="WARNING"):
            self.app.warm(threading.Event())
        self.assertEqual(self.google.geocode.call_count, 2)
        self.app.route(BODY)
        self.google.route.assert_called_once()

    def test_invalid_place_id_invalidated_without_same_request_retry(self):
        self.google.route.side_effect = npr.APIError(502, "REJECTED", "Google rejected the request.", ["origin"])
        with self.assertRaises(npr.APIError):
            self.app.route({"from": {"label": "home"}, "to": POINT})
        self.assertEqual(self.app.cache.rows, {})
        self.google.route.side_effect = None
        self.app.route({"from": {"label": "home"}, "to": POINT})
        self.assertEqual(self.google.geocode.call_count, 2)

    def test_leaving_now_does_not_send_a_local_clock_timestamp(self):
        def google_request(request, timeout):
            payload = json.loads(request.data)
            self.assertNotIn("departureTime", payload)
            self.assertEqual(payload["routingPreference"], "TRAFFIC_AWARE")
            return io.BytesIO(b'{"routes":[{"duration":"60s","distanceMeters":100}]}')
        self.app.google = npr.GoogleClient("fake-key", Mock(side_effect=google_request))
        # Regression: Google rejects explicit past timestamps. Even a skewed
        # local clock must not affect the leave-now request sent to Google.
        with patch.object(npr, "utc_now", return_value=NOW - timedelta(days=365)):
            result = self.app.route(BODY)
        self.assertEqual(result["driveMinutes"], 1.0)


class HTTPFixture(ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.server = npr.ThreadingHTTPServer(("127.0.0.1", 0), npr.handler_for(self.app))
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.worker.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, body=BODY, secret="5678"):
        return self.request("POST", "/route", json.dumps(body), {"Authorization": "Bearer " + secret, "Content-Type": "application/json"})


class HTTPTests(HTTPFixture):
    def test_locations_requires_authentication_and_never_calls_google(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            status, _, raw = self.request("GET", "/locations", headers=headers)
            self.assertEqual(status, 401)
            self.assertNotIn(b"10 Example St", raw)
        for secret in self.app.config.secrets:
            status, headers, raw = self.request("GET", "/locations", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw), {"locations": self.app.config.labels})
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-No-Part-Routes-API-Version"], docs.API_VERSION)
            self.assertNotIn(b"fake-google-key", raw)
            self.assertNotIn(b"secret with spaces", raw)
        self.google.geocode.assert_not_called()
        self.google.route.assert_not_called()

    def test_locations_preserves_configured_pluscode_and_reloads_without_restart(self):
        config = self.config_files("[HOME]\naddress=10 Example St\n[meeting]\npluscode=849VCWC8+R9\n[work]\nlatitude=29.76\nlongitude=-95.37\n")
        self.app.config = config
        status, _, raw = self.request("GET", "/locations", headers={"Authorization": "Bearer 5678"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"locations": {
            "home": {"address": "10 Example St"}, "meeting": {"pluscode": "849VCWC8+R9"}, "work": POINT}})
        path = self.directory / "locations.ini"
        path.write_text("[home]\naddress=20 Example St\n")
        os.utime(path, (1, 1))
        _, _, raw = self.request("GET", "/locations", headers={"Authorization": "Bearer 5678"})
        self.assertEqual(json.loads(raw), {"locations": {"home": {"address": "20 Example St"}}})
        self.google.geocode.assert_not_called()

    def test_locations_empty_and_method_restriction(self):
        self.app.config.labels = {}
        status, _, raw = self.request("GET", "/locations", headers={"Authorization": "Bearer 5678"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"locations": {}})
        status, headers, _ = self.request("POST", "/locations", "{}", {"Authorization": "Bearer 5678"})
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")

    def test_locations_duplicate_auth_rejected_and_openapi_requires_secret(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.putrequest("GET", "/locations")
            connection.putheader("Authorization", "Bearer 5678")
            connection.putheader("Authorization", "Bearer 5678")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()
        operation = docs.openapi()["paths"]["/locations"]["get"]
        self.assertEqual(operation["security"], [{"SharedSecret": []}])

    def test_success_has_exact_contract_and_version_headers(self):
        status, headers, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {"driveMinutes", "distanceMiles", "summary", "usesTolls", "avoidTolls", "avoidTollsError"})
        self.assertEqual(headers["X-No-Part-Routes-API-Version"], docs.API_VERSION)
        self.assertEqual(headers["X-No-Part-Routes-Docs-Version"], str(docs.DOCS_VERSION))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_pluscodes_http_and_discovery_report_current_api_version(self):
        code = "CWC8+R9 Mountain View, CA, USA"
        status, _, raw = self.post({"from": {"pluscode": code}, "to": POINT})
        self.assertEqual(status, 200)
        self.google.geocode.assert_called_once_with(code)
        self.assertEqual(set(json.loads(raw)), {"driveMinutes", "distanceMiles", "summary", "usesTolls", "avoidTolls", "avoidTollsError"})
        _, _, raw = self.request("GET", "/version")
        self.assertEqual(json.loads(raw)["apiVersion"], docs.API_VERSION)
        self.assertEqual(json.loads(raw)["docsVersion"], docs.DOCS_VERSION)
        _, _, raw = self.request("GET", "/openapi.json")
        endpoint = json.loads(raw)["paths"]["/route"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["from"]
        self.assertEqual({option["required"][0] for option in endpoint["oneOf"]}, {"label", "address", "pluscode", "coordinates"})
        _, _, raw = self.request("GET", "/docs")
        self.assertIn(b'"pluscode"', raw)
        self.assertIn(b"Documentation 2", raw)

    def test_public_endpoints_consistent_and_private_files_unavailable(self):
        for path in ("/version", "/docs", "/openapi.json", "/health"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn(b"fake-google-key", body)
                self.assertNotIn(b"10 Example St", body)
                self.assertNotIn(b"{{API_VERSION}}", body)
        status, _, raw = self.request("GET", "/version")
        self.assertEqual(json.loads(raw), docs.version_info())
        _, _, raw = self.request("GET", "/openapi.json")
        contract = json.loads(raw)
        self.assertEqual(contract["info"]["version"], docs.API_VERSION)
        self.assertEqual(contract["x-docs-version"], docs.DOCS_VERSION)
        for path in ("/settings.ini", "/apiKeys.ini", "/locations.ini", "/locationCache.csv", "/../settings.ini"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.google.route.assert_not_called()
        self.google.geocode.assert_not_called()

    def test_all_configured_keys_work_over_http(self):
        for secret in self.app.config.secrets:
            self.assertEqual(self.post(secret=secret)[0], 200)

    def test_unauthorized_before_google_and_errors_have_versions(self):
        status, headers, raw = self.post(secret="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["error"]["code"], "UNAUTHORIZED")
        self.assertIn("X-No-Part-Routes-Docs-Version", headers)
        self.google.route.assert_not_called()

    def test_methods_and_content_type(self):
        self.assertEqual(self.request("GET", "/route")[0], 405)
        self.assertEqual(self.request("POST", "/version", "")[0], 405)
        self.assertEqual(self.request("POST", "/route", "{}", {"Authorization": "Bearer 5678"})[0], 415)
        self.assertEqual(self.request("BREW", "/route")[0], 405)

    def test_invalid_json_duplicate_keys_and_nonfinite_numbers(self):
        headers = {"Authorization": "Bearer 5678", "Content-Type": "application/json"}
        for raw in ('{', '{"from":{},"from":{},"to":{}}', '{"from":{"coordinates":{"latitude":NaN,"longitude":0}},"to":{}}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/route", raw, headers)[0], 400)
        self.google.route.assert_not_called()

    def test_oversize_request(self):
        headers = {"Authorization": "Bearer 5678", "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/route", " " * (npr.MAX_BODY + 1), headers)[0], 413)
        self.google.route.assert_not_called()

    def test_upstream_error_is_sanitized_and_not_stale(self):
        self.post()
        self.google.route.side_effect = npr.APIError(504, "GOOGLE_TIMEOUT", "Google request timed out.")
        status, _, body = self.post()
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["error"]["code"], "GOOGLE_TIMEOUT")
        self.assertNotIn("driveMinutes", json.loads(body))

    def test_logs_do_not_contain_request_query_or_credentials(self):
        with self.assertLogs(npr.LOG, level="INFO") as captured:
            self.request("GET", "/unknown?address=private-home&key=very-secret")
            self.post(secret="very-secret")
        text = " ".join(captured.output)
        self.assertNotIn("private-home", text)
        self.assertNotIn("very-secret", text)

    def test_duplicate_authorization_content_length_and_chunked_are_rejected(self):
        def raw_request(extra_headers):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                connection.putrequest("POST", "/route")
                connection.putheader("Authorization", "Bearer 5678")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "2")
                for name, value in extra_headers:
                    connection.putheader(name, value)
                connection.endheaders(b"{}")
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()
        self.assertEqual(raw_request([("Authorization", "Bearer 5678")]), 401)
        self.assertEqual(raw_request([("Content-Length", "2")]), 400)
        self.assertEqual(raw_request([("Transfer-Encoding", "chunked")]), 400)
        self.google.route.assert_not_called()


class StartupTests(TemporaryTest):
    def test_sample_config_blocks_startup_and_check_before_runtime_files(self):
        self.copy_distribution()
        self.config_files()
        shutil.copyfile(self.directory / "apiKeys.example.ini", self.directory / "apiKeys.ini")
        for args in ([], ["--check"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(self.directory / "no_part_routes.py"), *args],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("Replace every sample secret", result.stdout)
                for name in ("logs", "usage.ini", "locationCache.csv"):
                    self.assertFalse((self.directory / name).exists())

    def copy_distribution(self):
        for name in ("no_part_routes.py", "api_documentation.py", "docs.html", "startWindows.bat",
                     "startLinux.sh", "settings.example.ini", "apiKeys.example.ini", "locations.example.ini"):
            shutil.copyfile(npr.BASE_DIR / name, self.directory / name)

    def test_preflight_from_another_directory_no_network(self):
        self.copy_distribution()
        config = self.config_files()
        # Choose an available local port instead of depending on fixed port 8080.
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        settings = self.directory / "settings.ini"
        settings.write_text(settings.read_text().replace("8080", str(port)), encoding="utf-8")
        result = subprocess.run([sys.executable, str(self.directory / "no_part_routes.py"), "--check"],
                                cwd=npr.BASE_DIR, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Google credentials were not tested", result.stdout)
        self.assertTrue((self.directory / "locationCache.csv").exists())

    def test_missing_config_and_occupied_port_are_actionable(self):
        self.copy_distribution()
        result = subprocess.run([sys.executable, str(self.directory / "no_part_routes.py"), "--check"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("settings.ini", result.stdout)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            self.config_files(settings=f"[server]\nhost=127.0.0.1\nport={port}\n[google]\napi_key=fake-key")
            result = subprocess.run([sys.executable, str(self.directory / "no_part_routes.py"), "--check"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("port is already in use", result.stdout)
        self.assertNotIn("fake-key", result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows launcher requires Windows")
    def test_windows_launcher_creates_examples_without_overwriting_existing_files(self):
        self.copy_distribution()
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        # Placeholder key causes an actionable failure, and newline dismisses pause.
        result = subprocess.run(["cmd", "/c", str(self.directory / "startWindows.bat"), "--check"],
                                cwd=npr.BASE_DIR, input="\n", env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Paste your Google API key", result.stdout)
        for name in ("settings", "apiKeys", "locations"):
            self.assertTrue((self.directory / f"{name}.ini").exists())
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        self.config_files(settings=f"[server]\nhost=127.0.0.1\nport={port}\n[google]\napi_key=fake-key")
        before = (self.directory / "apiKeys.ini").read_bytes()
        result = subprocess.run(["cmd", "/c", str(self.directory / "startWindows.bat"), "--check"],
                                cwd=npr.BASE_DIR, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.directory / "apiKeys.ini").read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "Linux launcher requires a POSIX shell")
    def test_linux_launcher_creates_examples(self):
        self.copy_distribution()
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(["sh", str(self.directory / "startLinux.sh"), "--check"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Paste your Google API key", result.stdout)
        for name in ("settings", "apiKeys", "locations"):
            self.assertTrue((self.directory / f"{name}.ini").exists())


if __name__ == "__main__":
    unittest.main()
