import os
import unittest
from unittest.mock import patch

import no_part_routes as npr
import test_service as fixtures


class LocationReloadTests(fixtures.ApplicationFixture):
    def setUp(self):
        super().setUp()
        self.app.config = self.config_files("[home]\naddress=10 Example St\n[work]\naddress=20 Example St\n")
        self.path = self.directory / "locations.ini"
        os.utime(self.path, (1, 1))
        self.original = self.app.locations()

    def save(self, content):
        self.path.write_text(content, encoding="utf-8")
        # Make the save stable without waiting or depending on filesystem resolution.
        os.utime(self.path, (1, 1))

    def test_changed_added_removed_labels_and_empty_file(self):
        self.save("[HOME]\npluscode=849VCWC8+R9\n[new]\nlatitude=29.76\nlongitude=-95.37\n")
        self.assertEqual(self.app.locations(), {"locations": {
            "home": {"pluscode": "849VCWC8+R9"}, "new": fixtures.POINT}})
        self.app.route({"from": {"label": "home"}, "to": {"label": "new"}})
        self.google.geocode.assert_called_once_with("849VCWC8+R9")
        self.save("; intentionally empty\n")
        self.assertEqual(self.app.locations(), {"locations": {}})
        with self.assertRaises(npr.APIError) as result:
            self.app.route({"from": {"label": "home"}, "to": fixtures.POINT})
        self.assertEqual(result.exception.code, "UNKNOWN_LABEL")

    def test_invalid_edits_retain_entire_snapshot_and_recover(self):
        for content in ("[home]\naddress=changed\n[work]\nlatitude=91\nlongitude=2\n",
                        "[unfinished", "[DEFAULT]\naddress=private\n[home]\n",
                        "[Home]\naddress=one\n[home]\naddress=two\n"):
            with self.subTest(content=content):
                self.save(content)
                with self.assertLogs(npr.LOG, level="WARNING") as logs:
                    self.assertEqual(self.app.locations(), self.original)
                    self.assertEqual(self.app.locations(), self.original)
                self.assertEqual(len(logs.output), 1)
                self.assertNotIn("private", logs.output[0])
        self.save("[home]\naddress=recovered\n")
        self.assertEqual(self.app.locations(), {"locations": {"home": {"address": "recovered"}}})

    def test_missing_and_unreadable_file_retain_snapshot(self):
        self.path.unlink()
        with self.assertLogs(npr.LOG, level="WARNING") as logs:
            self.assertEqual(self.app.locations(), self.original)
            self.assertEqual(self.app.locations(), self.original)
        self.assertEqual(len(logs.output), 1)
        self.save("[home]\naddress=recovered\n")
        self.assertEqual(self.app.locations()["locations"]["home"]["address"], "recovered")
        with patch.object(npr.Path, "read_bytes", side_effect=PermissionError()), self.assertLogs(npr.LOG, level="WARNING"):
            self.assertEqual(self.app.locations()["locations"]["home"]["address"], "recovered")

    def test_same_size_and_timestamp_edits_detected_by_content(self):
        self.save("[home]\naddress=30 Example St\n[work]\naddress=40 Example St\n")
        self.assertEqual(self.app.locations()["locations"]["home"]["address"], "30 Example St")

    def test_recent_save_waits_for_settle_interval(self):
        self.path.write_text("[home]\naddress=changed\n")
        modified = self.path.stat().st_mtime
        with patch.object(npr.time, "time", return_value=modified + 0.1):
            self.assertEqual(self.app.locations(), self.original)
        with patch.object(npr.time, "time", return_value=modified + 0.6):
            self.assertEqual(self.app.locations()["locations"]["home"]["address"], "changed")

    def test_edit_during_read_retries_on_next_request(self):
        self.save("[home]\naddress=changed\n")
        read = npr.Path.read_bytes

        def read_then_change(path):
            raw = read(path)
            self.save("[home]\naddress=changed again\n")
            return raw

        with patch.object(npr.Path, "read_bytes", read_then_change):
            self.assertEqual(self.app.locations(), self.original)
        self.assertEqual(self.app.locations()["locations"]["home"]["address"], "changed again")

    def test_inflight_route_uses_one_snapshot_and_next_route_uses_new_cache_keys(self):
        def geocode(address):
            self.save("[home]\naddress=30 Example St\n[work]\naddress=40 Example St\n")
            return "id-" + address

        self.google.geocode.side_effect = geocode
        body = {"from": {"label": "home"}, "to": {"label": "work"}}
        self.app.route(body)
        self.assertEqual(self.google.route.call_args.args, (
            {"placeId": "id-10 Example St"}, {"placeId": "id-20 Example St"}))
        self.app.route(body)
        self.assertEqual(self.google.route.call_args.args, (
            {"placeId": "id-30 Example St"}, {"placeId": "id-40 Example St"}))
        self.assertEqual(self.google.geocode.call_count, 4)

    def test_only_locations_reload_and_listing_makes_no_google_calls(self):
        (self.directory / "settings.ini").write_text("invalid")
        (self.directory / "apiKeys.ini").write_text("[other]\nsecret=changed\n")
        self.save("[home]\naddress=changed\n")
        self.assertEqual(self.app.locations()["locations"]["home"]["address"], "changed")
        self.assertTrue(self.app.authorized("Bearer 5678"))
        self.assertFalse(self.app.authorized("Bearer changed"))
        self.google.geocode.assert_not_called()
        self.google.route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
