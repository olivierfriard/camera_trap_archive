"""Check reverse-geocoding normalization, shared caching and upstream throttling."""

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from geocoding import GeocodingError, reverse_geocode


class GeocodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_path = Path(self.directory.name) / "geocoding.sqlite3"
        self.clock = 1000.0
        self.time_patch = patch("geocoding.time.time", side_effect=lambda: self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def lookup(self, latitude=45.0, longitude=7.6, service_url="https://example.test/reverse"):
        return reverse_geocode(
            latitude, longitude, cache_path=self.cache_path,
            service_url=service_url, user_agent="camera_trap_archive/test",
        )

    @staticmethod
    def response(address):
        return io.StringIO(json.dumps({
            "features": [{"properties": {"geocoding": address}}],
        }))

    def test_address_mapping_and_cache_hit_during_cooldown(self):
        with patch("geocoding.urlopen", return_value=self.response({
            "city": "Torino", "county": "Torino", "state": "Piemonte", "country": "Italia",
        })) as upstream:
            expected = dict(comune="Torino", provincia="Torino", regione="Piemonte", country="Italia")
            self.assertEqual(self.lookup(), expected)
            self.assertEqual(self.lookup(), expected)
            upstream.assert_called_once()
            request = upstream.call_args.args[0]
            self.assertIn("format=geocodejson", request.full_url)
            self.assertIn("accept-language=it", request.full_url)
            self.assertEqual(request.get_header("User-agent"), "camera_trap_archive/test")

    def test_other_coordinates_are_throttled_across_connections(self):
        with patch("geocoding.urlopen", side_effect=lambda *a, **k: self.response({"country": "Italia"})) as upstream:
            self.lookup()
            with self.assertRaises(GeocodingError) as raised:
                self.lookup(46.0)
            self.assertEqual(raised.exception.status, 429)
            self.assertGreaterEqual(raised.exception.retry_after, 1)
            self.assertEqual(upstream.call_count, 1)
            self.clock += 2
            self.lookup(46.0)
            self.assertEqual(upstream.call_count, 2)

    def test_administrative_fallbacks_and_partial_data(self):
        with patch("geocoding.urlopen", return_value=self.response({
            "admin": {"level8": "Comune", "level6": "Provincia", "level4": "Regione"},
        })):
            self.assertEqual(self.lookup(), dict(comune="Comune", provincia="Provincia", regione="Regione", country=""))

    def test_timeout_still_enforces_cooldown(self):
        with patch("geocoding.urlopen", side_effect=TimeoutError) as upstream:
            with self.assertRaises(GeocodingError) as raised:
                self.lookup()
            self.assertEqual(raised.exception.status, 503)
            with self.assertRaises(GeocodingError) as raised:
                self.lookup()
            self.assertEqual(raised.exception.status, 429)
            upstream.assert_called_once()

    def test_upstream_rejection_enforces_longer_cooldown(self):
        with patch("geocoding.urlopen", side_effect=HTTPError("https://example.test", 429, "Busy", {}, None)):
            with self.assertRaises(GeocodingError):
                self.lookup()
        self.clock += 2
        with patch("geocoding.urlopen") as upstream:
            with self.assertRaises(GeocodingError) as raised:
                self.lookup()
            self.assertEqual(raised.exception.status, 429)
            self.assertGreater(raised.exception.retry_after, 50)
            upstream.assert_not_called()

    def test_no_result_and_malformed_response(self):
        for response in ({"features": []}, {"features": [{"properties": {}}]}, None):
            with self.subTest(response=response):
                self.clock += 2
                with patch("geocoding.urlopen", return_value=io.StringIO(json.dumps(response))):
                    with self.assertRaises(GeocodingError) as raised:
                        self.lookup()
                    self.assertIn(raised.exception.status, (404, 503))

    def test_locked_cache_does_not_call_upstream(self):
        with patch("geocoding.urlopen", return_value=self.response({"country": "Italia"})):
            self.lookup()
        with sqlite3.connect(self.cache_path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with patch("geocoding.urlopen") as upstream:
                with self.assertRaises(GeocodingError) as raised:
                    self.lookup(46)
                self.assertEqual(raised.exception.status, 429)
                upstream.assert_not_called()

    def test_cache_expires_and_provider_switch_uses_new_cache_key(self):
        with patch("geocoding.urlopen", side_effect=lambda *a, **k: self.response({"country": "Italia"})) as upstream:
            self.lookup()
            self.clock += 31 * 86400
            self.lookup()
            self.clock += 2
            self.lookup(service_url="https://another.test/reverse")
            self.assertEqual(upstream.call_count, 3)


if __name__ == "__main__":
    unittest.main()
