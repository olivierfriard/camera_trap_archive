"""Check verified recovery, persistent retries and authenticated recovery requests."""

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

import archive_download
import video_conversion
from video_conversion import file_md5


class DownloadCopyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.remote = self.folder / 'remote.mp4'
        self.remote.write_bytes(b'verified MP4')
        self.md5 = file_md5(self.remote)
        self.output = self.folder / 'sample.mp4.compressed.mp4'

    def test_only_verified_contents_are_published(self):
        def copy(command, **kwargs):
            self.assertEqual(command[1:3], ['copyto', 'OF2:archive/sample.mp4'])
            self.assertFalse(self.output.exists())
            self.assertNotEqual(Path(command[3]), self.output)
            Path(command[3]).write_bytes(self.remote.read_bytes())
        with patch('archive_download.subprocess.run', side_effect=copy):
            archive_download.download_archive('OF2:archive/sample.mp4', self.output, self.md5)
        self.assertEqual(self.output.read_bytes(), self.remote.read_bytes())
        self.assertFalse(list(self.folder.glob('.archive-restore-*.part')))

    def test_bad_hash_and_failed_copy_never_publish_partial_file(self):
        for fails in (False, True):
            def copy(command, **kwargs):
                Path(command[3]).write_bytes(b'partial MP4')
                if fails:
                    raise subprocess.TimeoutExpired(command, 1)
            with patch('archive_download.subprocess.run', side_effect=copy):
                with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                    archive_download.download_archive('OF2:archive/sample.mp4', self.output, self.md5)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.folder.glob('.archive-restore-*.part')))

    def test_existing_verified_file_is_reused_and_other_contents_are_preserved(self):
        self.output.write_bytes(self.remote.read_bytes())
        with patch('archive_download.subprocess.run') as run:
            archive_download.download_archive('OF2:archive/sample.mp4', self.output, self.md5)
            run.assert_not_called()
        self.output.write_bytes(b'other contents')
        with self.assertRaises(ValueError):
            archive_download.download_archive('OF2:archive/sample.mp4', self.output, self.md5)
        self.assertEqual(self.output.read_bytes(), b'other contents')

    def test_real_rclone_download_to_local_directory(self):
        binary = shutil.which('rclone') or str(Path.home() / '.local/bin/rclone')
        if not Path(binary).is_file():
            self.skipTest('rclone not installed')
        archive_download.download_archive(str(self.remote), self.output, self.md5, rclone_binary=binary, timeout=30)
        self.assertEqual(file_md5(self.output), self.md5)


@unittest.skipUnless(os.environ.get('CAMERATRAP_TEST_POSTGRES') == '1', 'Opt-in PostgreSQL tests')
class DownloadQueuePostgresTests(unittest.TestCase):
    """Use temporary tables and a rollback without touching production rows."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / '.env'))
        self.database = create_engine(make_url(config['db_url']).set(password=None))
        self.addCleanup(self.database.dispose)
        self.conn = self.database.connect()
        self.addCleanup(self.conn.close)
        transaction = self.conn.begin()
        self.addCleanup(transaction.rollback)
        for table in ('video_conversions', 'archive_transfers', 'archive_downloads'):
            self.conn.execute(text(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP'))
        for table in ('photo_archive_transfers', 'photo_archive_downloads'):
            self.conn.execute(text(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP'))
        self.conn.execute(text('CREATE TEMP TABLE sighting (id INTEGER PRIMARY KEY, operator TEXT) ON COMMIT DROP'))
        self.conn.execute(text('CREATE TEMP TABLE media (sighting_id INTEGER, new_file_name TEXT) ON COMMIT DROP'))
        self.output = self.folder / 'sample.avi.compressed.mp4'
        self.remote = self.folder / 'remote.mp4'
        self.remote.write_bytes(b'archive MP4')
        self.conn.execute(text('''
            INSERT INTO video_conversions
                (source_filename, operator, source_md5, output_filename, output_md5, status)
            VALUES ('sample.avi', 'user', 'source-hash', :output, :md5, 'completed')
        '''), dict(output=self.output.name, md5=file_md5(self.remote)))
        self.conn.execute(text('''
            INSERT INTO archive_transfers (source_filename, output_filename, output_md5, destination, status)
            VALUES ('sample.avi', :output, :md5, :remote, 'completed')
        '''), dict(output=self.output.name, md5=file_md5(self.remote), remote=str(self.remote)))
        self.conn.execute(text("INSERT INTO sighting VALUES (1, 'user')"))
        self.conn.execute(text("INSERT INTO media VALUES (1, 'sample.avi')"))
        connection = self.conn

        class TransactionEngine:
            @contextmanager
            def begin(self):
                with connection.begin_nested():
                    yield connection

            @contextmanager
            def connect(self):
                yield connection

        self.engine = TransactionEngine()
        config['log_file'] = str(self.folder / 'app.log')
        with patch('dotenv.dotenv_values', return_value=config):
            import main
        self.main = main
        self.account = dict(username='user', fullname='User', institution='Test', code='T', is_active=True, is_admin=False)
        for patcher in (patch.object(main, 'engine', self.engine),
                        patch.object(main, 'get_user', return_value=self.account),
                        patch.object(main, 'start_conversion_worker'),
                        patch.dict(main.app.config, TESTING=True, UPLOAD_FOLDER=str(self.folder))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session['username'] = 'user'
        self.status_url = main.APP_ROOT + '/video_conversion_status/sample.avi'
        status = self.client.get(self.status_url).json
        self.restore_url = status['restore_url']
        self.headers = {'X-CSRF-Token': status['restore_csrf_token']}

    def job(self):
        return self.conn.execute(text('SELECT * FROM archive_downloads')).mappings().one()

    def test_request_is_idempotent_and_worker_publishes_download(self):
        for _ in range(2):
            self.assertEqual(self.client.post(self.restore_url, headers=self.headers).status_code, 202)
        self.assertEqual(self.job()['attempts'], 0)
        self.assertEqual(self.conn.execute(text('SELECT COUNT(*) FROM archive_downloads')).scalar(), 1)
        self.assertEqual(self.client.get(self.status_url).json['restore_status'], 'pending')
        with patch('archive_download.download_archive', side_effect=lambda remote, output, md5, **kw: output.write_bytes(self.remote.read_bytes())):
            self.assertTrue(archive_download.process_one_download(self.engine, self.folder))
        self.assertEqual(self.job()['status'], 'completed')
        status = self.client.get(self.status_url).json
        self.assertTrue(status['download_url'].endswith(self.output.name))
        self.assertIsNone(status['restore_url'])
        self.assertEqual(status['transfer_status'], 'completed')
        with self.client.get(status['download_url']) as response:
            self.assertEqual(response.data, self.remote.read_bytes())
        self.assertEqual(self.client.post(self.restore_url, headers=self.headers).status_code, 200)
        self.output.unlink()
        self.assertEqual(self.client.post(self.restore_url, headers=self.headers).status_code, 202)
        self.assertEqual(self.job()['status'], 'pending')

    def test_access_requires_csrf_and_owner_of_saved_verified_video(self):
        self.assertEqual(self.client.post(self.restore_url).status_code, 400)
        for sql in ["UPDATE video_conversions SET operator='other'",
                    "UPDATE sighting SET operator='other'",
                    "UPDATE archive_transfers SET status='pending'",
                    "DELETE FROM media"]:
            nested = self.conn.begin_nested()
            self.conn.execute(text(sql))
            self.assertEqual(self.client.post(self.restore_url, headers=self.headers).status_code, 404)
            nested.rollback()
        self.assertEqual(self.conn.execute(text('SELECT COUNT(*) FROM archive_downloads')).scalar(), 0)
        anonymous = self.main.app.test_client()
        self.assertEqual(anonymous.post(self.restore_url, headers=self.headers).status_code, 302)

    def test_failed_downloads_retry_then_allow_explicit_retry(self):
        self.client.post(self.restore_url, headers=self.headers)
        with patch('archive_download.download_archive', side_effect=OSError('network unavailable')):
            for attempt in range(1, 4):
                self.conn.execute(text('UPDATE archive_downloads SET next_attempt_at=CURRENT_TIMESTAMP'))
                self.assertTrue(archive_download.process_one_download(self.engine, self.folder))
                self.assertEqual(self.job()['attempts'], attempt)
                self.assertEqual(self.job()['status'], 'failed' if attempt == 3 else 'pending')
                self.assertFalse(archive_download.process_one_download(self.engine, self.folder))
        self.assertFalse(self.output.exists())
        self.client.post(self.restore_url, headers=self.headers)
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.job()['attempts'], 0)

    def test_worker_recovers_interrupted_job_and_avoids_duplicate_copy(self):
        self.client.post(self.restore_url, headers=self.headers)
        self.conn.execute(text("UPDATE archive_downloads SET status='processing', attempts=1"))
        self.output.write_bytes(self.remote.read_bytes())
        archive_download.recover_downloads(self.conn)
        self.assertTrue(archive_download.process_one_download(self.engine, self.folder))
        self.assertEqual(self.job()['status'], 'completed')
        self.assertEqual(self.job()['attempts'], 2)

    def test_active_request_does_not_reset_attempts(self):
        self.client.post(self.restore_url, headers=self.headers)
        self.conn.execute(text("UPDATE archive_downloads SET status='processing', attempts=1"))
        self.client.post(self.restore_url, headers=self.headers)
        self.assertEqual(self.job()['status'], 'processing')
        self.assertEqual(self.job()['attempts'], 1)

    def test_shared_worker_processes_recovery(self):
        self.client.post(self.restore_url, headers=self.headers)
        guard = MagicMock()
        guard.execute.return_value.scalar.return_value = True

        @contextmanager
        def connect():
            yield guard

        self.engine.connect = connect
        with patch('video_conversion.process_one', return_value=False), patch('archive_download.download_archive', side_effect=lambda remote, output, md5, **kw: output.write_bytes(b'archive MP4')):
            video_conversion.run_worker(self.engine, self.folder)
        self.assertEqual(self.job()['status'], 'completed')
        self.assertTrue(self.output.is_file())
