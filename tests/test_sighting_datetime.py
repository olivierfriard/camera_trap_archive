"""Verify editable sighting dates reach timestamp storage and survive errors."""

import tempfile
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values


class SightingDateTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory()
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / ".env"))
        config["log_file"] = str(Path(cls.logs.name) / "app.log")
        with patch("dotenv.dotenv_values", return_value=config):
            import main
        cls.main = main

    @classmethod
    def tearDownClass(cls):
        cls.logs.cleanup()

    def setUp(self):
        self.connection = MagicMock()
        self.connection.execute.side_effect = self.execute
        self.inserts = []
        self.duplicate = False
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = self.connection
        account = dict(username="user", fullname="User", institution="Test", code="T", is_active=True, is_admin=False)
        for patcher in (
            patch.object(self.main, "engine", engine),
            patch.object(self.main, "get_user", return_value=account),
            patch.object(self.main, "extract_frame", return_value=b"frame"),
            patch.dict(self.main.app.config, TESTING=True, SIGHTING_TIMEZONE="Europe/Rome"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "user"

    def execute(self, statement, parameters=None):
        result = MagicMock()
        sql = str(statement).strip()
        if sql.startswith("SELECT COUNT"):
            result.scalar.return_value = int(self.duplicate)
        elif sql.startswith("INSERT INTO sighting"):
            self.inserts.append(parameters)
            result.scalar.return_value = 42
        result.mappings.return_value.all.return_value = []
        result.mappings.return_value.fetchone.return_value = None
        return result

    def post(self, date, time):
        return self.client.post(self.main.APP_ROOT + "/save_info", data={
            "date": date, "time_": time, "new_file_name": "sample.mp4",
            "original_file_name": "original.mp4", "file_content_md5": "test-md5",
            "code": "TEST", "wolf_number": "2", "scalp": "C1", "note": "Keep these notes",
        })

    def assert_editable_values(self, response, date, time):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.fields = {}

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if tag == "input" and attributes.get("name") in ("date", "time_"):
                    name = attributes["name"]
                    if name in self.fields:
                        raise AssertionError("Duplicate date/time form key")
                    self.fields[name] = attributes

        inputs = Inputs()
        inputs.feed(response.get_data(as_text=True))
        for name, value in (("date", date), ("time_", time)):
            self.assertEqual(inputs.fields[name]["type"], "text")
            self.assertEqual(inputs.fields[name]["form"], "uploadForm")
            self.assertEqual(inputs.fields[name]["value"], value)

    def test_valid_edited_timestamp_and_local_offsets(self):
        for date, time, expected, offset in (
            ("2026-11-13", "12:34:56", datetime(2026, 11, 13, 12, 34, 56), 1),
            ("2026-07-01", "12:34", datetime(2026, 7, 1, 12, 34), 2),
        ):
            with self.subTest(date=date, time=time):
                self.assertEqual(self.post(date, time).status_code, 302)
                timestamp = self.inserts[-1]["timestamp"]
                self.assertEqual(timestamp.replace(tzinfo=None), expected)
                self.assertEqual(timestamp.utcoffset(), timedelta(hours=offset))

    def test_unknown_date_and_time_remain_nullable(self):
        self.assertEqual(self.post("", "").status_code, 302)
        self.assertIsNone(self.inserts[-1]["timestamp"])

    def test_invalid_and_incomplete_values_preserve_form_without_writes(self):
        for date, time in (
            ("2026-02-30", "12:00:00"), ("2026-11-13", "25:00:00"),
            ("2026-11-13", ""), ("", "12:00"), ("2026-03-29", "02:30"),
            ('"><script>alert(1)</script>', "12:00"),
        ):
            with self.subTest(date=date, time=time):
                response = self.post(date, time)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Data o ora non valide", response.get_data(as_text=True))
                self.assertIn("Keep these notes", response.get_data(as_text=True))
                self.assert_editable_values(response, date, time)
                self.assertEqual(self.inserts, [])
        self.connection.commit.assert_not_called()

    def test_duplicate_code_preserves_edited_values(self):
        self.duplicate = True
        response = self.post("2026-11-14", "09:10:11")
        self.assertEqual(response.status_code, 200)
        self.assert_editable_values(response, "2026-11-14", "09:10:11")
        self.assertEqual(self.inserts, [])
        self.connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
