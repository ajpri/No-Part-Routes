import concurrent.futures
from datetime import timedelta
import io
import json
import logging
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import no_part_routes as npr
import test_service as fixtures

TemporaryTest = fixtures.TemporaryTest
NOW = fixtures.NOW


class UsageTests(TemporaryTest):
    def guard(self, routes=2, geocoding=2, now=lambda: NOW):
        return npr.UsageGuard(self.directory / "usage.ini", routes, geocoding, now=now)

    def test_separate_limits_and_restart_persistence(self):
        guard = self.guard()
        guard.reserve("routes")
        guard.reserve("routes")
        guard = self.guard()
        with self.assertLogs(npr.LOG, level="WARNING"), self.assertRaises(npr.APIError) as error:
            guard.reserve("routes")
        self.assertEqual(error.exception.status, 429)
        guard.reserve("geocoding")
        self.assertEqual(guard.counts, {"routes": 2, "geocoding": 1})

    def test_midnight_reset_and_backward_clock_do_not_reset_usage(self):
        guard = self.guard(routes=1)
        guard.reserve("routes")
        guard.now = lambda: NOW - timedelta(days=1)
        with self.assertLogs(npr.LOG, level="WARNING"), self.assertRaises(npr.APIError):
            guard.reserve("routes")
        guard.now = lambda: NOW + timedelta(days=1)
        guard.reserve("routes")
        self.assertEqual(guard.day, (NOW + timedelta(days=1)).date().isoformat())
        self.assertEqual(guard.counts, {"routes": 1, "geocoding": 0})

    def test_concurrent_calls_cannot_exceed_cap(self):
        guard = self.guard(routes=5)
        def attempt(index):
            try:
                guard.reserve("routes")
                return True
            except npr.APIError:
                return False
        with self.assertLogs(npr.LOG, level="WARNING"), concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            accepted = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(accepted), 5)
        self.assertEqual(self.guard(routes=5).counts["routes"], 5)

    def test_attempt_count_survives_upstream_failure_and_blocked_call_has_no_network(self):
        guard = self.guard(routes=1)
        opener = Mock(side_effect=TimeoutError)
        client = npr.GoogleClient("private-google-key", opener, usage=guard)
        with self.assertLogs(npr.LOG, level="WARNING"), self.assertRaises(npr.APIError) as error:
            client.route({}, {})
        self.assertEqual(error.exception.status, 504)
        with self.assertLogs(npr.LOG, level="WARNING"), self.assertRaises(npr.APIError) as error:
            client.route({}, {})
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(self.guard().counts["routes"], 1)

    def test_failed_counter_write_prevents_google_call(self):
        guard = self.guard()
        opener = Mock()
        client = npr.GoogleClient("private-key", opener, usage=guard)
        with patch.object(guard, "_save", side_effect=PermissionError), self.assertLogs(npr.LOG, level="ERROR"), self.assertRaises(npr.APIError) as error:
            client.route({}, {})
        self.assertEqual(error.exception.code, "USAGE_STORAGE_UNAVAILABLE")
        opener.assert_not_called()
        self.assertEqual(guard.counts["routes"], 0)

    def test_bad_usage_file_fails_closed_and_does_not_expose_contents(self):
        for content in ("private-malformed-content", "[daily]\ndate=bad\nroutes=0\ngeocoding=0", "[daily]\ndate=2026-09-30\nroutes=-1\ngeocoding=0"):
            with self.subTest(content=content):
                (self.directory / "usage.ini").write_text(content)
                with self.assertRaises(npr.ConfigError) as error:
                    self.guard()
                self.assertNotIn("private-malformed-content", str(error.exception))

    def test_geocoding_charged_separately_and_success_reused_from_cache(self):
        guard = self.guard()
        opener = Mock(return_value=io.BytesIO(b'{"status":"OK","results":[{"place_id":"id"}]}'))
        client = npr.GoogleClient("private-key", opener, usage=guard)
        cache = npr.LocationCache(self.directory / "locationCache.csv")
        cache.resolve("private-address", client.geocode)
        cache.resolve("private-address", client.geocode)
        self.assertEqual(guard.counts, {"routes": 0, "geocoding": 1})
        self.assertEqual(opener.call_count, 1)

    def test_logging_and_limits_config_defaults_and_custom_values(self):
        config = self.config_files()
        self.assertEqual((config.daily_routes_limit, config.daily_geocoding_limit), (150, 300))
        settings = self.directory / "settings.ini"
        settings.write_text(settings.read_text() + "\n[logging]\nmax_bytes=2048\nbackup_count=2\n[usage]\ndaily_routes_limit=10\ndaily_geocoding_limit=20\n")
        config = npr.Config.load(self.directory)
        self.assertEqual((config.log_max_bytes, config.log_backup_count, config.daily_routes_limit, config.daily_geocoding_limit), (2048, 2, 10, 20))
        settings.write_text(settings.read_text().replace("daily_routes_limit=10", "daily_routes_limit=0"))
        with self.assertRaises(npr.ConfigError):
            npr.Config.load(self.directory)


class LogFileTests(TemporaryTest):
    def test_rotation_is_bounded_and_request_ids_are_written(self):
        config = npr.Config("127.0.0.1", 8080, "key", ("5678",), {}, log_max_bytes=256, log_backup_count=2)
        handler = npr.configure_file_logging(self.directory, config)
        try:
            token = npr.REQUEST_ID.set("test-request-id")
            try:
                for index in range(20):
                    npr.LOG.info("event=test index=%d", index)
            finally:
                npr.REQUEST_ID.reset(token)
            paths = list((self.directory / "logs").glob("application.log*"))
            self.assertLessEqual(len(paths), 3)
            text = (self.directory / "logs" / "application.log").read_text()
            self.assertIn("request_id=test-request-id", text)
            self.assertRegex(text, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        finally:
            npr.LOG.removeHandler(handler)
            handler.close()


class LoggingHTTPTests(fixtures.HTTPFixture):
    def test_request_id_correlates_google_usage_and_client_without_private_data(self):
        handler = npr.configure_file_logging(self.directory, self.app.config)
        try:
            guard = npr.UsageGuard(self.directory / "usage.ini", 1, 1)
            opener = Mock(return_value=io.BytesIO(b'{"routes":[{"duration":"60s","distanceMeters":100}]}'))
            self.app.google = npr.GoogleClient("private-google-key", opener, usage=guard)
            status, headers, _ = self.post(secret="secret with spaces")
            self.assertEqual(status, 200)
            request_id = headers["X-No-Part-Routes-Request-ID"]
            text = (self.directory / "logs" / "application.log").read_text()
            self.assertIn("request_id=" + request_id, text)
            self.assertIn("event=google_call_reserved api=routes daily_count=1", text)
            self.assertIn("endpoint=/route client_key_index=3 status=200", text)
            for private in ("private-google-key", "secret with spaces", "29.76", "-95.37", "10 Example St"):
                self.assertNotIn(private, text)
            status, _, raw = self.post()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(raw)["error"]["code"], "DAILY_USAGE_LIMIT")
            self.assertEqual(opener.call_count, 1)
        finally:
            npr.LOG.removeHandler(handler)
            handler.close()
