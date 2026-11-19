"""Check camera defaults, edited coordinate snapshots and failed-form preservation."""

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values


class SightingCoordinatesTests(unittest.TestCase):
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
        self.camera = dict(codice="FT01", tipo="Camera", nome="Name", cognome="Surname", comune="Torino", provincia="Torino", regione="Piemonte", latitudine=45.0703, longitudine=7.6869)
        self.inserts = []
        self.statements = []
        self.duplicate = False
        self.connection = MagicMock()
        self.connection.execute.side_effect = self.execute
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = self.connection
        user = dict(username="user", fullname="User", institution="Test", code="T", is_active=True, is_admin=False)
        for patcher in (
            patch.object(self.main, "engine", engine), patch.object(self.main, "get_user", return_value=user),
            patch.object(self.main, "extract_frame", return_value=b"image"),
            patch.object(self.main, "start_conversion_worker"), patch.dict(self.main.app.config, TESTING=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "user"

    def execute(self, statement, parameters=None):
        sql = str(statement).strip()
        self.statements.append(sql)
        result = MagicMock()
        result.mappings.return_value.fetchone.return_value = None
        result.mappings.return_value.all.return_value = [{"codice": "FT01"}]
        if sql.startswith("SELECT * FROM fototrappole"):
            self.assertEqual(parameters["operator"], "user")
            if parameters["codice"] == "FT01":
                result.mappings.return_value.fetchone.return_value = self.camera
        elif sql.startswith("SELECT COUNT"):
            result.scalar.return_value = int(self.duplicate)
        elif sql.startswith("INSERT INTO sighting"):
            self.inserts.append(dict(parameters))
            result.scalar.return_value = 42
        return result

    def post(self, **values):
        data = dict(camtrap_id="FT01", code="TEST", date="", time_="", new_file_name="sample.mp4", original_file_name="original.mp4", file_content_md5="hash", wolf_number="1", scalp="C1", note="Keep notes")
        data.update(values)
        return self.client.post(self.main.APP_ROOT + "/save_info", data=data)

    def test_absent_coordinate_fields_copy_camera_defaults(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.inserts[0]["latitude"], self.inserts[0]["longitude"]), (45.0703, 7.6869))

    def test_optional_pup_number_and_anomaly_are_saved_for_video(self):
        for value, expected in (('', None), ('0', 0), ('3', 3)):
            with self.subTest(value=value):
                self.assertEqual(self.post(pup_number=value, anomaly='Zoppia\nArto posteriore').status_code, 302)
                self.assertEqual(self.inserts[-1]['pup_number'], expected)
                self.assertEqual(self.inserts[-1]['anomaly'], 'Zoppia\nArto posteriore')
        self.assertEqual(self.post(anomaly='  ').status_code, 302)
        self.assertIsNone(self.inserts[-1]['anomaly'])

    def test_invalid_pup_number_keeps_video_form_without_inserting(self):
        for value in ('-1', '1.5', 'abc', '2147483648'):
            with self.subTest(value=value):
                response = self.post(pup_number=value, anomaly='<draft>')
                html = response.get_data(as_text=True)
                self.assertIn('Il numero di cuccioli', html)
                self.assertIn('value="' + value + '"', html)
                self.assertIn('&lt;draft&gt;', html)
                self.assertEqual(self.inserts, [])

    def test_edited_coordinates_are_saved_without_updating_camera(self):
        original = dict(self.camera)
        response = self.post(latitude="0", longitude="-15.25", operator="someone-else")
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.inserts[0]["latitude"], self.inserts[0]["longitude"]), (0.0, -15.25))
        self.assertEqual(self.camera, original)
        self.assertFalse(any(sql.startswith("UPDATE fototrappole") for sql in self.statements))

    def test_blank_pair_remains_unknown(self):
        self.assertEqual(self.post(latitude="", longitude="").status_code, 302)
        self.assertIsNone(self.inserts[0]["latitude"])
        self.assertIsNone(self.inserts[0]["longitude"])

    def test_invalid_and_partial_pairs_return_form_without_inserting(self):
        for latitude, longitude in (("45", ""), ("", "7"), ("nan", "7"), ("45", "inf"), ("91", "7"), ("45", "181"), ("abc", "7")):
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.post(latitude=latitude, longitude=longitude)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Coordinate non valide", response.get_data(as_text=True))
                self.assertEqual(self.inserts, [])
        self.connection.commit.assert_not_called()

    def test_unavailable_camera_is_rejected_even_with_explicit_coordinates(self):
        response = self.post(camtrap_id="PRIVATE", latitude="45", longitude="7")
        self.assertEqual(response.status_code, 200)
        self.assertIn("non è disponibile per il tuo utente", response.get_data(as_text=True))
        self.assertEqual(self.inserts, [])

    def test_failed_submission_keeps_edits_and_unique_form_keys(self):
        self.duplicate = True
        response = self.post(latitude="44.123", longitude="8.456")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.fields = {}

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if tag == "input" and attributes.get("name") in ("latitude", "longitude"):
                    name = attributes["name"]
                    if name in self.fields:
                        raise AssertionError("Duplicate coordinate key")
                    self.fields[name] = attributes

        parser = Inputs()
        parser.feed(html)
        self.assertEqual(parser.fields["latitude"]["value"], "44.123")
        self.assertEqual(parser.fields["longitude"]["value"], "8.456")
        self.assertIn('data-preserve-coordinates="true"', html)
        self.assertIn('data-latitude="45.0703" data-longitude="7.6869"', html)
        self.assertIn('id="sightingMapPanel" class="mt-3" hidden', html)
        self.assertEqual(self.inserts, [])


if __name__ == "__main__":
    unittest.main()
