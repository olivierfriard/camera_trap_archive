"""Verify AVI previews preserve originals and do not block sighting entry."""

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values


class VideoPreviewTests(unittest.TestCase):
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
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        config = patch.dict(self.main.app.config, UPLOAD_FOLDER=str(self.folder), TESTING=True)
        config.start()
        self.addCleanup(config.stop)
        logger = patch.object(self.main.app.logger, "disabled", True)
        logger.start()
        self.addCleanup(logger.stop)
        context = self.main.app.test_request_context("/")
        context.push()
        self.addCleanup(context.pop)

    def test_mp4_is_served_without_conversion(self):
        with patch.object(self.main.subprocess, "run") as run:
            self.assertEqual(
                self.main.video_preview_url("sample.mp4", create=True),
                self.main.url_for("uploaded_file", filename="sample.mp4"),
            )
            run.assert_not_called()

    def test_cached_avi_preview_is_reused(self):
        (self.folder / "sample.AVI").write_bytes(b"original")
        (self.folder / "sample.AVI.preview.mp4").write_bytes(b"preview")
        with patch.object(self.main.subprocess, "run") as run:
            self.assertEqual(
                self.main.video_preview_url("sample.AVI", create=True),
                self.main.url_for("uploaded_file", filename="sample.AVI.preview.mp4"),
            )
            run.assert_not_called()

    def test_missing_preview_does_not_convert_on_form_retry(self):
        (self.folder / "sample.avi").write_bytes(b"original")
        with patch.object(self.main.subprocess, "run") as run:
            self.assertIsNone(self.main.video_preview_url("sample.avi"))
            run.assert_not_called()

    def test_conversion_failures_clean_up_and_preserve_original(self):
        source = self.folder / "sample.avi"
        source.write_bytes(b"original")
        for error in (
            FileNotFoundError("ffmpeg unavailable"),
            subprocess.TimeoutExpired("ffmpeg", 15),
            subprocess.CalledProcessError(1, "ffmpeg"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.main.subprocess, "run", side_effect=error):
                    self.assertIsNone(self.main.video_preview_url(source.name, create=True))
                self.assertEqual(list(self.folder.iterdir()), [source])
                self.assertEqual(source.read_bytes(), b"original")

    def test_preview_failure_still_renders_sighting_form(self):
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.fetchone.return_value = None
        connection.execute.return_value.mappings.return_value.all.return_value = []
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        account = dict(username="user", fullname="User", institution="Test", code="T", is_active=True, is_admin=False)
        client = self.main.app.test_client()
        with client.session_transaction() as session:
            session["username"] = "user"
        original = b"uploaded original bytes"
        with (
            patch.object(self.main, "engine", engine),
            patch.object(self.main, "get_user", return_value=account),
            patch.object(self.main.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 15)),
            patch.object(self.main.camtrap_banner_decoder, "extract_date_time", return_value={"date": "2026-11-13", "time": "123456"}) as extract,
        ):
            response = client.post(
                self.main.APP_ROOT + "/upload_video",
                data={"video": (io.BytesIO(original), "original.avi")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Anteprima non disponibile", html)
        self.assertIn("Scarica il video originale", html)
        self.assertIn('id="uploadForm"', html)
        self.assertIn(hashlib.md5(original).hexdigest(), html)
        self.assertEqual(extract.call_args.args[0].read_bytes(), original)
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_real_conversion_and_duplicate_sighting_form(self):
        source = self.folder / "sample.avi"
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "0.4", "-c:v", "mjpeg", "-threads", "1", "-c:a", "pcm_s16le", str(source),
        ], check=True, capture_output=True, timeout=15)
        original = source.read_bytes()
        url = self.main.video_preview_url(source.name, create=True)
        preview = self.folder / "sample.avi.preview.mp4"
        self.assertEqual(url, self.main.url_for("uploaded_file", filename=preview.name))
        self.assertEqual(source.read_bytes(), original)
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(preview),
        ], check=True, capture_output=True, text=True, timeout=10)
        streams = json.loads(result.stdout)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual((video["width"], video["height"]), (160, 120))
        self.assertEqual(audio["codec_name"], "aac")
        data = preview.read_bytes()
        self.assertLess(data.index(b"moov"), data.index(b"mdat"))

        account = dict(username="user", fullname="User", institution="Test", code="T", is_active=True, is_admin=False)
        connection = MagicMock()
        connection.execute.return_value.scalar.return_value = 1
        connection.execute.return_value.mappings.return_value.all.return_value = []
        connection.execute.return_value.mappings.return_value.fetchone.return_value = SimpleNamespace(
            tipo="Test", nome="Name", cognome="Surname", comune="Torino", provincia="Torino", regione="Piemonte",
        )
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        client = self.main.app.test_client()
        with client.session_transaction() as session:
            session["username"] = "user"
        with (
            patch.object(self.main, "get_user", return_value=account),
            patch.object(self.main, "engine", engine),
            patch.object(self.main.subprocess, "run") as convert,
        ):
            response = client.get(url, headers={"Range": "bytes=0-127"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.mimetype, "video/mp4")
            self.assertEqual(response.data, data[:128])
            response.close()
            response = client.post(self.main.APP_ROOT + "/save_info", data={
                "code": "DUPLICATE", "new_file_name": source.name, "camtrap_id": "FT01",
                "original_file_name": "original.avi", "file_content_md5": hashlib.md5(original).hexdigest(),
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'src="{url}"', response.get_data(as_text=True))
            self.assertIn('name="new_file_name" value="sample.avi"', response.get_data(as_text=True))
            convert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
