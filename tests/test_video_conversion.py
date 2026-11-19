"""Check archive quality, persistent job transitions and output publication."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import io
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import video_conversion


class ArchiveConversionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def test_command_matches_rust_compression_settings(self):
        source = self.folder / "original.avi"
        source.write_bytes(b"original")
        target = self.folder / "original.avi.compressed.mp4"

        def encode(command, **options):
            self.assertEqual(command[0], "/opt/media tools/ffmpeg")
            self.assertEqual(options["timeout"], 3600)
            for option, value in (
                ("-c:v", "libx264"), ("-preset", "medium"), ("-crf", "24"),
                ("-c:a", "aac"), ("-b:a", "128k"), ("-movflags", "+faststart"),
                ("-map_metadata", "0"), ("-map_metadata:s", "0:s"), ("-map_chapters", "0"),
            ):
                self.assertEqual(command[command.index(option) + 1], value)
            self.assertNotIn("-vf", command)
            self.assertNotEqual(Path(command[-1]), target)
            Path(command[-1]).write_bytes(b"complete mp4")

        with patch("video_conversion.subprocess.run", side_effect=encode), \
                patch("video_conversion.is_easy_video_compressed", return_value=False):
            video_conversion.convert_video(source, target, ffmpeg_binary="/opt/media tools/ffmpeg")
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"complete mp4")
        self.assertEqual(set(self.folder.iterdir()), {source, target})

    def test_timeout_keeps_original_and_does_not_publish_partial_mp4(self):
        source = self.folder / "original.mp4"
        source.write_bytes(b"original")
        target = self.folder / "original.mp4.compressed.mp4"

        def fail(command, **options):
            Path(command[-1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired("ffmpeg", options["timeout"])

        with patch("video_conversion.subprocess.run", side_effect=fail), \
                patch("video_conversion.is_easy_video_compressed", return_value=False):
            with self.assertRaises(subprocess.TimeoutExpired):
                video_conversion.convert_video(source, target)
        self.assertEqual(list(self.folder.iterdir()), [source])

    def test_compressor_tag_requires_exact_key_and_value(self):
        for metadata, expected in (
            (';FFMETADATA1\nEasy Video Compressor=Compressed with easy_video_compressor\n', True),
            (';FFMETADATA1\nEASY VIDEO COMPRESSOR=Compressed with easy_video_compressor\n', True),
            (';FFMETADATA1\nEASY_VIDEO_COMPRESSOR=Compressed with easy_video_compressor\n', True),
            (';FFMETADATA1\ncomment=Compressed with easy_video_compressor\n', False),
            (';FFMETADATA1\nEasy Video Compressor=another program\n', False),
            (';FFMETADATA1\ntitle=example\\\nEasy Video Compressor=Compressed with easy_video_compressor\n', False),
            (';FFMETADATA1\ntitle=example\u2028Easy Video Compressor=Compressed with easy_video_compressor\n', False),
            (';FFMETADATA1\n[CHAPTER]\nEasy Video Compressor=Compressed with easy_video_compressor\n', False),
        ):
            with self.subTest(metadata=metadata), patch('video_conversion.subprocess.run') as run:
                run.return_value.stdout = metadata
                self.assertEqual(video_conversion.is_easy_video_compressed(
                    self.folder/'source.mp4', ffmpeg_binary='/opt/media tools/ffmpeg'), expected)
                self.assertEqual(run.call_args.args[0][0], '/opt/media tools/ffmpeg')
                self.assertNotIn('libx264', run.call_args.args[0])

    def test_tag_read_failure_keeps_original_without_encoding(self):
        source = self.folder / 'source.mp4'
        source.write_bytes(b'original')
        target = self.folder / (source.name + '.compressed.mp4')
        with patch('video_conversion.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 30)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                video_conversion.convert_video(source, target)
            run.assert_called_once()
        self.assertEqual(list(self.folder.iterdir()), [source])

    def test_failed_copy_verification_keeps_original_and_removes_temporary(self):
        source = self.folder / 'source.mp4'
        source.write_bytes(b'original')
        target = self.folder / (source.name + '.compressed.mp4')
        with patch('video_conversion.is_easy_video_compressed', return_value=True), \
                patch('video_conversion.shutil.copyfile', side_effect=lambda src, dst: dst.write_bytes(b'partial')):
            with self.assertRaisesRegex(OSError, 'copy failed verification'):
                video_conversion.convert_video(source, target)
        self.assertEqual(list(self.folder.iterdir()), [source])
        self.assertEqual(source.read_bytes(), b'original')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_real_compressor_mp4_is_copied_byte_for_byte_with_archive_name(self):
        source = self.folder / 'upload.mp4'
        subprocess.run([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=80x40:rate=10', '-t', '0.2', '-c:v', 'libx264', '-threads', '1',
            '-metadata', 'Easy Video Compressor=Compressed with easy_video_compressor',
            '-movflags', '+use_metadata_tags', str(source),
        ], check=True, capture_output=True, timeout=15)
        target = self.folder / (source.name + '.compressed.mp4')
        self.assertTrue(video_conversion.is_easy_video_compressed(source))
        with patch('video_conversion.subprocess.run', wraps=subprocess.run) as run:
            video_conversion.convert_video(source, target)
        run.assert_called_once()  # Metadata inspection only; no encoding subprocess.
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.name, 'upload.mp4.compressed.mp4')
        self.assertEqual(set(self.folder.iterdir()), {source, target})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools are required')
    def test_tagged_other_container_is_remuxed_without_encoding(self):
        source = self.folder / 'upload.mkv'
        subprocess.run([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=80x40:rate=10', '-t', '0.2', '-c:v', 'libx264', '-threads', '1',
            '-metadata', 'Easy Video Compressor=Compressed with easy_video_compressor', str(source),
        ], check=True, capture_output=True, timeout=15)
        target = self.folder / (source.name + '.compressed.mp4')
        original_hash = video_conversion.file_md5(source)
        with patch('video_conversion.subprocess.run', wraps=subprocess.run) as run:
            video_conversion.convert_video(source, target)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-c')+1], 'copy')
        self.assertNotIn('libx264', command)
        self.assertTrue(video_conversion.is_easy_video_compressed(target))
        self.assertEqual(video_conversion.file_md5(source), original_hash)
        packets = []
        for path in (source, target):
            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_packets', '-show_data_hash', 'sha256', '-of', 'json', str(path)],
                check=True, capture_output=True, text=True, timeout=10)
            packets.append([packet['data_hash'] for packet in json.loads(result.stdout)['packets']])
        self.assertEqual(packets[0], packets[1])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_real_mp4_compression_keeps_resolution_and_metadata(self):
        source = self.folder / "original.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=800x450:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "0.3", "-c:v", "libx264", "-threads", "1", "-c:a", "aac",
            "-metadata", "title=Archive test", str(source),
        ], check=True, capture_output=True, timeout=15)
        original_md5 = video_conversion.file_md5(source)
        target = self.folder / "original.mp4.compressed.mp4"
        video_conversion.convert_video(source, target)
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target),
        ], check=True, capture_output=True, text=True, timeout=10)
        probe = json.loads(result.stdout)
        video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual((video["width"], video["height"]), (800, 450))
        self.assertEqual(audio["codec_name"], "aac")
        self.assertEqual(probe["format"]["tags"]["title"], "Archive test")
        self.assertEqual(video_conversion.file_md5(source), original_md5)
        data = target.read_bytes()
        self.assertLess(data.index(b"moov"), data.index(b"mdat"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_real_avi_without_audio_keeps_original_resolution(self):
        source = self.folder / "original.avi"
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=800x450:rate=10", "-t", "0.2",
            "-c:v", "mjpeg", "-threads", "1", str(source),
        ], check=True, capture_output=True, timeout=15)
        original_md5 = video_conversion.file_md5(source)
        target = self.folder / "original.avi.compressed.mp4"
        video_conversion.convert_video(source, target)
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(target),
        ], check=True, capture_output=True, text=True, timeout=10)
        streams = json.loads(result.stdout)["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["codec_name"], "h264")
        self.assertEqual((streams[0]["width"], streams[0]["height"]), (800, 450))
        self.assertEqual(video_conversion.file_md5(source), original_md5)


class ConversionRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / ".env"))
        config["log_file"] = str(Path(self.directory.name) / "app.log")
        with patch("dotenv.dotenv_values", return_value=config):
            import main
        self.main = main
        self.start_worker = main.start_conversion_worker
        self.job = {"status": "pending", "output_filename": "sample.mp4.compressed.mp4"}
        self.parameters = []
        self.connection = MagicMock()

        def execute(sql, parameters=None):
            self.parameters.append((str(sql), parameters))
            result = MagicMock()
            result.scalar.return_value = 0 if "SELECT COUNT" in str(sql) else 42
            result.mappings.return_value.fetchone.return_value = self.job if "FROM video_conversions" in str(sql) else None
            result.mappings.return_value.all.return_value = []
            return result

        self.connection.execute.side_effect = execute
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = self.connection
        engine.begin.return_value.__enter__.return_value = self.connection
        account = dict(username="user", fullname="User", institution="Test", code="T", is_active=True, is_admin=False)
        for patcher in (
            patch.object(main, "engine", engine), patch.object(main, "get_user", return_value=account),
            patch.object(main, "extract_frame", return_value=b"frame"),
            patch.object(main, "start_conversion_worker"),
            patch.object(main.app.logger, "disabled", True),
            patch.dict(main.app.config, TESTING=True, UPLOAD_FOLDER=self.directory.name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "user"

    def test_status_is_authenticated_scoped_and_exposes_only_completed_file(self):
        url = self.main.APP_ROOT + "/video_conversion_status/sample.mp4"
        anonymous = self.main.app.test_client()
        self.assertEqual(anonymous.get(url).status_code, 302)
        self.assertIsNone(self.client.get(url).json["download_url"])
        self.assertEqual(self.parameters[-1][1]["operator"], "user")
        self.job["status"] = "completed"
        self.job["transfer_status"] = "completed"
        self.assertIsNone(self.client.get(url).json["download_url"])
        output = Path(self.directory.name) / self.job["output_filename"]
        output.write_bytes(b"local MP4")
        response = self.client.get(url)
        self.assertTrue(response.json["download_url"].endswith("sample.mp4.compressed.mp4"))
        output.unlink()
        response = self.client.get(url)
        self.assertIsNone(response.json["download_url"])
        self.assertEqual(response.json["transfer_status"], "completed")
        self.job = None
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_saved_media_links_completed_conversion_and_keeps_upload_identity(self):
        self.job["status"] = "completed"
        response = self.client.post(self.main.APP_ROOT + "/save_info", data={
            "new_file_name": "sample.mp4", "original_file_name": "original.mp4",
            "file_content_md5": "original-hash", "code": "TEST", "date": "", "time_": "",
        })
        self.assertEqual(response.status_code, 302)
        media = next(parameters for sql, parameters in self.parameters if "INSERT INTO media" in sql)
        self.assertEqual(media["new_file_name"], "sample.mp4")
        self.assertEqual(media["file_content_md5"], "original-hash")
        self.assertEqual(media["converted_file_name"], "sample.mp4.compressed.mp4")
        self.assertTrue(any("FOR UPDATE" in sql for sql, _ in self.parameters))

    def test_upload_queues_job_and_starts_worker_without_running_archive_conversion(self):
        with (
            patch.object(self.main, "video_preview_url", return_value=None),
            patch.object(self.main, "start_conversion_worker") as start,
            patch.object(video_conversion, "convert_video") as archive,
            patch.object(self.main.camtrap_banner_decoder, "extract_date_time", return_value={"date": "", "time": ""}),
        ):
            for _ in range(2):
                response = self.client.post(self.main.APP_ROOT + "/upload_video", data={
                    "video": (io.BytesIO(b"uploaded video"), "original.mp4"),
                }, content_type="multipart/form-data")
                self.assertEqual(response.status_code, 200)
            self.assertEqual(start.call_count, 2)
            archive.assert_not_called()
        jobs = [parameters for sql, parameters in self.parameters if "INSERT INTO video_conversions" in sql]
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]["source"], jobs[1]["source"])
        self.assertTrue(all(job["output"].endswith(".mp4.compressed.mp4") for job in jobs))

    def test_worker_launcher_detaches_process_without_waiting(self):
        with patch.object(self.main.app, "instance_path", self.directory.name), patch.object(self.main.subprocess, "Popen") as spawn:
            self.start_worker()
        command = spawn.call_args.args[0]
        self.assertTrue(Path(command[1]).is_file())
        self.assertEqual(command[-1], "--watch")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        spawn.return_value.wait.assert_not_called()


@unittest.skipUnless(os.environ.get("CAMERATRAP_TEST_POSTGRES") == "1", "Opt-in PostgreSQL tests")
class QueuePostgresTests(unittest.TestCase):
    """Use temporary tables and an outer rollback; production rows are untouched."""

    def setUp(self):
        config = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
        self.database = create_engine(make_url(config["db_url"]).set(password=None))
        self.conn = self.database.connect()
        self.transaction = self.conn.begin()
        self.conn.execute(text("CREATE TEMP TABLE video_conversions (LIKE public.video_conversions INCLUDING ALL) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE archive_transfers (LIKE public.archive_transfers INCLUDING ALL) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE archive_downloads (LIKE public.archive_downloads INCLUDING ALL) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE media (id INTEGER PRIMARY KEY, new_file_name TEXT, converted_file_name TEXT) ON COMMIT DROP"))
        for table in ('photo_archive_transfers', 'photo_archive_downloads'):
            self.conn.execute(text(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP'))
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.source = self.folder / "original.avi"
        self.source.write_bytes(b"original")
        connection = self.conn

        class TransactionEngine:
            @contextmanager
            def begin(self):
                with connection.begin_nested():
                    yield connection

        self.engine = TransactionEngine()

    def tearDown(self):
        self.transaction.rollback()
        self.conn.close()
        self.database.dispose()
        self.directory.cleanup()

    def enqueue(self):
        video_conversion.enqueue_conversion(self.conn, self.source.name, "test-user", video_conversion.file_md5(self.source))

    def job(self):
        return self.conn.execute(text("SELECT * FROM video_conversions")).mappings().one()

    def test_enqueue_is_idempotent_and_completion_links_existing_media(self):
        self.enqueue()
        self.enqueue()
        self.conn.execute(text("INSERT INTO media (id, new_file_name) VALUES (1, 'original.avi')"))
        with patch("video_conversion.convert_video", side_effect=lambda source, target, **kw: target.write_bytes(b"mp4")):
            self.assertTrue(video_conversion.process_one(self.engine, self.folder))
        self.assertEqual(self.job()["status"], "completed")
        self.assertEqual(self.job()["attempts"], 1)
        self.assertEqual(self.job()["output_md5"], video_conversion.file_md5(self.folder / self.job()["output_filename"]))
        self.assertEqual(self.conn.execute(text("SELECT converted_file_name FROM media")).scalar(), self.job()["output_filename"])
        self.assertFalse(video_conversion.process_one(self.engine, self.folder))

    def test_failures_retry_then_stop_after_three_attempts(self):
        self.enqueue()
        with patch("video_conversion.convert_video", side_effect=OSError("conversion failed")):
            for attempt in range(1, 4):
                self.conn.execute(text("UPDATE video_conversions SET next_attempt_at = CURRENT_TIMESTAMP"))
                self.assertTrue(video_conversion.process_one(self.engine, self.folder))
                self.assertEqual(self.job()["attempts"], attempt)
                self.assertEqual(self.job()["status"], "failed" if attempt == 3 else "pending")
                if attempt < 3:
                    self.assertFalse(video_conversion.process_one(self.engine, self.folder))
        self.assertFalse(video_conversion.process_one(self.engine, self.folder))

    def test_published_file_is_reused_after_worker_restart(self):
        self.enqueue()
        output = self.folder / self.job()["output_filename"]
        output.write_bytes(b"previously published")
        with patch("video_conversion.convert_video") as convert:
            self.assertTrue(video_conversion.process_one(self.engine, self.folder))
            convert.assert_not_called()
        self.assertEqual(self.job()["status"], "completed")

    def test_source_hash_change_is_rejected(self):
        self.enqueue()
        self.source.write_bytes(b"changed")
        with patch("video_conversion.convert_video") as convert:
            self.assertTrue(video_conversion.process_one(self.engine, self.folder))
            convert.assert_not_called()
        self.assertEqual(self.job()["status"], "pending")
        self.assertIn("hash", self.job()["last_error"])

    def test_worker_recovers_interrupted_conversion(self):
        self.enqueue()
        self.conn.execute(text("UPDATE video_conversions SET status = 'processing', attempts = 1"))
        output = self.folder / self.job()["output_filename"]
        output.write_bytes(b"previously published")
        engine = self.engine
        guard = MagicMock()
        guard.execute.return_value.scalar.return_value = True

        @contextmanager
        def connect():
            yield guard

        engine.connect = connect
        video_conversion.run_worker(engine, self.folder)
        self.assertEqual(self.job()["status"], "completed")
        self.assertEqual(self.job()["attempts"], 2)

    def test_advisory_lock_prevents_another_worker(self):
        key = video_conversion.WORKER_LOCK + os.getpid()
        self.conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            with patch.object(video_conversion, "WORKER_LOCK", key), patch.object(video_conversion, "process_one") as process:
                video_conversion.run_worker(self.database, self.folder)
                process.assert_not_called()
        finally:
            self.conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


if __name__ == "__main__":
    unittest.main()
