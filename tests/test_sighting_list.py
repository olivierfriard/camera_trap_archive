"""Check sighting list access, ordering, data and cached-preview fallbacks."""

import base64
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, text


class SightingListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory()
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / ".env"))
        config["log_file"] = str(Path(cls.logs.name) / "app.log")
        with patch("dotenv.dotenv_values", return_value=config):
            import main
        cls.main = main
        sqlite3.register_converter("SIGHTING_TEST_TIMESTAMP", lambda value: datetime.fromisoformat(value.decode()))

    @classmethod
    def tearDownClass(cls):
        cls.logs.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.database = create_engine("sqlite://", connect_args={"detect_types": sqlite3.PARSE_DECLTYPES})
        self.addCleanup(self.database.dispose)
        with self.database.begin() as conn:
            conn.execute(text("""
                CREATE TABLE sighting (
                    id INTEGER PRIMARY KEY, code TEXT, timestamp SIGHTING_TEST_TIMESTAMP,
                    camtrap_id TEXT, wolf_number INTEGER, scalp TEXT, latitude REAL,
                    longitude REAL, notes TEXT, operator TEXT
                )
            """))
            conn.execute(text("CREATE TABLE media (id INTEGER PRIMARY KEY, sighting_id INTEGER, image BLOB, new_file_name TEXT)"))
        account = dict(username="user", fullname="Nome Utente", institution="Test", code="T", is_active=True, is_admin=False)
        for patcher in (
            patch.object(self.main, "engine", self.database),
            patch.object(self.main, "get_user", return_value=account),
            patch.object(self.main.app.logger, "disabled", True),
            patch.dict(self.main.app.config, TESTING=True, UPLOAD_FOLDER=str(self.folder), SIGHTING_TIMEZONE="Europe/Rome"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "user"

    def add_sighting(self, id=1, code="TEST", timestamp="2026-11-15T12:34:56+00:00", operator="user", image=b"database image", filename="sample.mp4", notes="Nota di prova"):
        with self.database.begin() as conn:
            conn.execute(text("""
                INSERT INTO sighting VALUES
                    (:id, :code, :timestamp, 'FT01', 0, 'C1', 0, 7.6, :notes, :operator)
            """), dict(id=id, code=code, timestamp=timestamp, notes=notes, operator=operator))
            conn.execute(text("INSERT INTO media VALUES (:id, :id, :image, :filename)"), dict(id=id, image=image, filename=filename))

    def get_list(self):
        response = self.client.get(self.main.APP_ROOT + "/sighting_list?operator=someone-else")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_preview_player_includes_data_and_does_not_start_conversion(self):
        self.add_sighting()
        (self.folder / "sample.mp4").write_bytes(b"original")
        (self.folder / "sample.mp4.preview-640.mp4").write_bytes(b"cached preview")
        with patch.object(self.main.subprocess, "run") as encode:
            html = self.get_list()
            encode.assert_not_called()
        self.assertIn('<video class="sighting-media"', html)
        self.assertIn("sample.mp4.preview-640.mp4", html)
        self.assertIn('controls playsinline preload="none"', html)
        self.assertIn("15/11/2026 13:34:56 CET", html)
        for value in ("FT01", "C1", "Nota di prova", "Latitudine", "Longitudine", "Apri scheda"):
            self.assertIn(value, html)
        self.assertIn("<dd>0</dd>", html)
        self.assertIn("<dd>0.0</dd>", html)
        self.assertNotIn("Anteprima video non disponibile", html)
        with self.main.app.test_request_context():
            self.assertIn(self.main.url_for("view", sighting_id=1), html)

    def test_missing_preview_uses_original_database_image(self):
        image = b"stored JPEG frame"
        self.add_sighting(image=image)
        html = self.get_list()
        self.assertIn('<img class="sighting-media"', html)
        self.assertIn("data:image/jpeg;base64," + base64.b64encode(image).decode(), html)
        self.assertIn("Anteprima video non disponibile", html)
        self.assertNotIn('<video class="sighting-media"', html)

    def test_missing_metadata_and_image_render_without_errors(self):
        self.add_sighting(timestamp=None, image=None, filename=None, notes=None)
        html = self.get_list()
        self.assertIn("Non indicato", html)
        self.assertIn("Immagine non disponibile", html)
        self.assertIn("Anteprima video non disponibile", html)
        self.assertIn("Nessuna nota", html)
        self.assertNotIn("base64,None", html)

    def test_user_filter_and_reverse_chronological_order(self):
        self.add_sighting(id=1, code="OLDER", timestamp="2026-01-01T12:00:00+00:00")
        self.add_sighting(id=2, code="NEWEST")
        self.add_sighting(id=3, code="UNDATED", timestamp=None)
        self.add_sighting(id=4, code="PRIVATE", operator="someone-else")
        self.add_sighting(id=5, code="SAME-DATE-LATER-ID")
        html = self.get_list()
        self.assertNotIn("PRIVATE", html)
        self.assertLess(html.index("SAME-DATE-LATER-ID"), html.index("NEWEST"))
        self.assertLess(html.index("NEWEST"), html.index("OLDER"))
        self.assertLess(html.index("OLDER"), html.index("UNDATED"))

    def test_notes_are_escaped_and_footer_identifies_user(self):
        self.add_sighting(notes="<script>alert('test')</script>\nSeconda riga")
        html = self.get_list()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Seconda riga", html)
        self.assertIn("Utente: Nome Utente", html)

    def test_empty_list_and_authentication(self):
        self.assertIn("Nessun avvistamento registrato", self.get_list())
        response = self.main.app.test_client().get(self.main.APP_ROOT + "/sighting_list")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))


if __name__ == "__main__":
    unittest.main()
