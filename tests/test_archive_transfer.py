"""Check archive eligibility, copy integrity and persistent transfer retries."""

import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import archive_transfer
from video_conversion import file_md5
import video_conversion


class ArchiveCopyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "sample.mp4.compressed.mp4"
        self.source.write_bytes(b"compressed content")
        self.md5 = file_md5(self.source)

    def test_storage_normalization_requires_a_remote(self):
        for value in ("OF2/test_camera_trap_archive", "OF2:test_camera_trap_archive/"):
            self.assertEqual(archive_transfer.normalize_archive_storage(value), "OF2:test_camera_trap_archive")
        for value in (None, "", "folder", "/tmp/archive", "OF2:../folder", "OF2:/absolute"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive_transfer.normalize_archive_storage(value)

    def test_copy_requires_matching_remote_md5(self):
        with patch("archive_transfer.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 3, stdout="", stderr="Not found"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=self.md5 + "  sample.mp4.compressed.mp4\n"),
            ]
            archive_transfer.copy_archive(self.source, "OF2:archive/sample.mp4.compressed.mp4", self.md5)
            command = run.call_args_list[1].args[0]
            self.assertEqual(command[1], "copyto")
            self.assertIn("--checksum", command)
            self.assertIn("--immutable", command)
            self.assertEqual(run.call_args_list[2].args[0][1], "md5sum")
            self.assertTrue(self.source.exists())

    def test_mismatched_remote_hash_is_an_error(self):
        with patch("archive_transfer.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 3, stdout="", stderr="Not found"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout="0" * 32 + "  sample.mp4\n"),
            ]
            with self.assertRaisesRegex(ValueError, "Remote MD5"):
                archive_transfer.copy_archive(self.source, "OF2:archive/sample.mp4", self.md5)

    def test_changed_local_file_and_non_archive_input_never_transfer(self):
        with patch("archive_transfer.subprocess.run") as run:
            with self.assertRaises(ValueError):
                archive_transfer.copy_archive(self.source, "OF2:archive/sample.mp4", "incorrect")
            preview = self.source.with_name("sample.preview-640.mp4")
            preview.write_bytes(b"preview")
            with self.assertRaises(ValueError):
                archive_transfer.copy_archive(preview, "OF2:archive/sample.mp4", file_md5(preview))
            run.assert_not_called()

    @unittest.skipUnless(shutil.which("rclone"), "rclone is required")
    def test_real_rclone_local_copy_and_idempotent_retry(self):
        destination = Path(self.directory.name) / "archive.mp4"
        for _ in range(2):
            archive_transfer.copy_archive(self.source, str(destination), self.md5, rclone_binary=shutil.which("rclone"))
        self.assertEqual(file_md5(destination), self.md5)
        destination.write_bytes(b"different remote content")
        with self.assertRaisesRegex(ValueError, "different content"):
            archive_transfer.copy_archive(self.source, str(destination), self.md5, rclone_binary=shutil.which("rclone"), timeout=30)
        self.assertEqual(destination.read_bytes(), b"different remote content")


@unittest.skipUnless(os.environ.get("CAMERATRAP_TEST_POSTGRES") == "1", "Opt-in PostgreSQL tests")
class ArchiveQueuePostgresTests(unittest.TestCase):
    """Keep all test rows in temporary tables inside a rolled-back transaction."""

    def setUp(self):
        config = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
        self.database = create_engine(make_url(config["db_url"]).set(password=None))
        self.conn = self.database.connect()
        self.transaction = self.conn.begin()
        self.conn.execute(text("CREATE TEMP TABLE video_conversions (LIKE public.video_conversions INCLUDING ALL) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE archive_transfers (LIKE public.archive_transfers INCLUDING ALL) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE media (new_file_name TEXT, converted_file_name TEXT, sighting_id INTEGER) ON COMMIT DROP"))
        self.conn.execute(text("CREATE TEMP TABLE sighting (id INTEGER PRIMARY KEY, operator TEXT) ON COMMIT DROP"))
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.output = self.folder / "sample.avi.compressed.mp4"
        self.output.write_bytes(b"compressed content")
        self.conn.execute(text("""
            INSERT INTO video_conversions
                (source_filename, operator, source_md5, output_filename, output_md5, status)
            VALUES ('sample.avi', 'test-user', 'original-hash', :filename, :md5, 'completed')
        """), {"filename": self.output.name, "md5": file_md5(self.output)})
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

    def save_and_enqueue(self):
        self.conn.execute(text("INSERT INTO sighting VALUES (1, 'test-user')"))
        self.conn.execute(text("INSERT INTO media VALUES ('sample.avi', 'sample.avi.compressed.mp4', 1)"))
        archive_transfer.enqueue_ready_transfers(self.conn, "OF2/test_camera_trap_archive")

    def job(self):
        return self.conn.execute(text("SELECT * FROM archive_transfers")).mappings().one()

    def test_unsaved_and_other_user_sightings_are_not_transferred(self):
        archive_transfer.enqueue_ready_transfers(self.conn, "OF2/folder")
        self.assertEqual(self.conn.execute(text("SELECT count(*) FROM archive_transfers")).scalar(), 0)
        self.conn.execute(text("INSERT INTO sighting VALUES (1, 'someone-else')"))
        self.conn.execute(text("INSERT INTO media VALUES ('sample.avi', 'sample.avi.compressed.mp4', 1)"))
        archive_transfer.enqueue_ready_transfers(self.conn, "OF2/folder")
        self.assertEqual(self.conn.execute(text("SELECT count(*) FROM archive_transfers")).scalar(), 0)

    def test_completed_conversion_is_enqueued_once_with_stable_destination(self):
        self.save_and_enqueue()
        expected = "OF2:test_camera_trap_archive/sample.avi.compressed.mp4"
        self.assertEqual(self.job()["destination"], expected)
        archive_transfer.enqueue_ready_transfers(self.conn, "OF2/another-folder")
        self.assertEqual(self.job()["destination"], expected)
        with patch("archive_transfer.copy_archive") as copy:
            self.assertTrue(archive_transfer.process_one_transfer(self.engine, self.folder))
            copy.assert_called_once()
        self.assertEqual(self.job()["status"], "completed")
        self.assertEqual(self.job()["attempts"], 1)
        self.assertFalse(archive_transfer.process_one_transfer(self.engine, self.folder))
        self.assertTrue(self.output.is_file())

    def test_failed_transfers_retry_and_keep_local_file(self):
        self.save_and_enqueue()
        with patch("archive_transfer.copy_archive", side_effect=OSError("network unavailable")):
            for attempt in range(1, 4):
                self.conn.execute(text("UPDATE archive_transfers SET next_attempt_at = CURRENT_TIMESTAMP"))
                self.assertTrue(archive_transfer.process_one_transfer(self.engine, self.folder))
                self.assertEqual(self.job()["attempts"], attempt)
                self.assertEqual(self.job()["status"], "failed" if attempt == 3 else "pending")
                self.assertTrue(self.output.is_file())
                self.assertFalse(archive_transfer.process_one_transfer(self.engine, self.folder))

    def test_interrupted_transfer_is_recovered(self):
        self.save_and_enqueue()
        self.conn.execute(text("UPDATE archive_transfers SET status='processing', attempts=1"))
        archive_transfer.recover_transfers(self.conn)
        with patch("archive_transfer.copy_archive"):
            self.assertTrue(archive_transfer.process_one_transfer(self.engine, self.folder))
        self.assertEqual(self.job()["status"], "completed")
        self.assertEqual(self.job()["attempts"], 2)

    def test_shared_worker_processes_saved_archive(self):
        self.save_and_enqueue()
        guard = MagicMock()
        guard.execute.return_value.scalar.return_value = True

        @contextmanager
        def connect():
            yield guard

        self.engine.connect = connect
        with patch("archive_transfer.copy_archive") as copy:
            video_conversion.run_worker(self.engine, self.folder, archive_storage="OF2/test_camera_trap_archive")
        copy.assert_called_once()
        self.assertEqual(self.job()["status"], "completed")

    def test_status_route_reports_saved_and_transfer_state(self):
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / ".env"))
        config["log_file"] = str(self.folder / "app.log")
        with patch("dotenv.dotenv_values", return_value=config):
            import main
        connection = self.conn

        @contextmanager
        def connect():
            yield connection

        self.engine.connect = connect
        account = dict(username="test-user", fullname="Test", institution="Test", code="T", is_active=True, is_admin=False)
        client = main.app.test_client()
        with client.session_transaction() as session:
            session["username"] = account["username"]
        url = main.APP_ROOT + "/video_conversion_status/sample.avi"
        with patch.object(main, "engine", self.engine), patch.object(main, "get_user", return_value=account):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["saved"])
            self.assertIsNone(response.json["transfer_status"])
            self.save_and_enqueue()
            response = client.get(url)
            self.assertTrue(response.json["saved"])
            self.assertEqual(response.json["transfer_status"], "pending")
            account["username"] = "someone-else"
            self.assertEqual(client.get(url).status_code, 404)


if __name__ == "__main__":
    unittest.main()
