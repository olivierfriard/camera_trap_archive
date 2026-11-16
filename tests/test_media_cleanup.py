"""Verify expiry boundaries, retained previews and recovery-safe local cleanup."""

import os
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import text

import media_cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.source = self.folder / 'sample.avi'
        self.compressed = self.folder / 'sample.avi.compressed.mp4'
        self.preview = self.folder / 'sample.avi.preview-640.mp4'
        for path in (self.source, self.compressed, self.preview):
            path.write_bytes(path.name.encode())
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        self.job = dict(source_filename=self.source.name, output_filename=self.compressed.name,
                        archived_at=self.now - timedelta(days=3))
        self.recovery = None
        connection = MagicMock()

        def execute(statement, parameters=None):
            result = MagicMock()
            if 'FROM archive_downloads' in str(statement):
                result.mappings.return_value.fetchone.return_value = self.recovery
            elif parameters:
                result.mappings.return_value.fetchone.return_value = self.job
            else:
                result.mappings.return_value.all.return_value = [dict(self.job)]
            return result

        connection.execute.side_effect = execute
        self.engine = MagicMock()
        self.engine.connect.return_value.__enter__.return_value = connection
        self.engine.begin.return_value.__enter__.return_value = connection
        logger = patch.object(media_cleanup.LOGGER, 'disabled', True)
        logger.start()
        self.addCleanup(logger.stop)

    def cleanup(self, **kwargs):
        kwargs.setdefault('archive_retention', timedelta(hours=48))
        kwargs.setdefault('recovery_retention', timedelta(hours=24))
        return media_cleanup.cleanup_local_videos(self.engine, self.folder, now=self.now, **kwargs)

    def test_default_is_dry_run_and_preserves_all_files(self):
        result = self.cleanup()
        self.assertEqual(result['eligible_files'], 2)
        self.assertEqual(result['deleted_files'], 0)
        self.assertTrue(all(path.exists() for path in (self.source, self.compressed, self.preview)))

    def test_custom_retention_controls_expiry(self):
        self.job['archived_at'] = self.now - timedelta(hours=50)
        self.recovery = dict(status='completed', finished_at=self.now - timedelta(hours=10))
        self.assertEqual(self.cleanup(
            apply=True, archive_retention=timedelta(hours=60), recovery_retention=timedelta(hours=12),
        )['deleted_files'], 0)
        self.assertEqual(self.cleanup(
            apply=True, archive_retention=timedelta(hours=60), recovery_retention=timedelta(hours=6),
        )['deleted_files'], 1)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.preview.exists())

    def test_invalid_duration_is_rejected_before_accessing_database(self):
        for key in ('archive_retention', 'recovery_retention'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.cleanup(apply=True, **{key: timedelta(0)})
        self.engine.connect.assert_not_called()
        self.assertTrue(self.source.exists())
        self.assertTrue(self.compressed.exists())

    def test_initial_files_expire_at_48_hours_and_never_delete_preview(self):
        self.job['archived_at'] = self.now - timedelta(hours=48) + timedelta(seconds=1)
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 0)
        self.job['archived_at'] -= timedelta(seconds=1)
        expected_bytes = self.source.stat().st_size + self.compressed.stat().st_size
        result = self.cleanup(apply=True)
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['deleted_bytes'], expected_bytes)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.compressed.exists())
        self.assertTrue(self.preview.is_file())
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 0)

    def test_recovered_mp4_retains_24_hours_even_with_old_mtime(self):
        os.utime(self.compressed, (1, 1))
        self.recovery = dict(status='completed', finished_at=self.now - timedelta(hours=24) + timedelta(seconds=1))
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 1)
        self.assertTrue(self.compressed.is_file())
        self.recovery['finished_at'] -= timedelta(seconds=1)
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 1)
        self.assertTrue(self.preview.is_file())

    def test_recovered_mp4_uses_recovery_expiry_instead_of_archive_expiry(self):
        self.job['archived_at'] = self.now - timedelta(hours=30)
        self.recovery = dict(status='completed', finished_at=self.now - timedelta(hours=25))
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 1)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.compressed.exists())

    def test_pending_or_processing_recovery_retains_both_videos(self):
        for status in ('pending', 'processing'):
            self.recovery = dict(status=status, finished_at=None)
            result = self.cleanup(apply=True)
            self.assertEqual(result['deleted_files'], 0)
            self.assertEqual(result['skipped_jobs'], 1)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.compressed.exists())

    def test_recovered_video_without_completion_date_is_retained(self):
        self.recovery = dict(status='completed', finished_at=None)
        self.assertEqual(self.cleanup(apply=True)['deleted_files'], 1)
        self.assertTrue(self.compressed.exists())

    def test_original_is_retained_without_a_current_nonempty_preview(self):
        for state in ('missing', 'empty', 'stale'):
            with self.subTest(state=state):
                self.preview.unlink(missing_ok=True)
                if state == 'empty':
                    self.preview.write_bytes(b'')
                elif state == 'stale':
                    self.preview.write_bytes(b'old preview')
                    os.utime(self.preview, (1, 1))
                result = self.cleanup(apply=True)
                self.assertTrue(self.source.exists())
                self.assertEqual(result['errors'], 0)

    def test_malformed_metadata_cannot_target_preview_or_external_paths(self):
        for filename in ('../sample.mp4', '/tmp/sample.mp4', self.preview.name):
            with self.subTest(filename=filename):
                self.job['source_filename'] = filename
                self.job['output_filename'] = filename + '.compressed.mp4'
                result = self.cleanup(apply=True)
                self.assertEqual(result['errors'], 1)
                self.assertTrue(self.source.exists())
                self.assertTrue(self.compressed.exists())
                self.assertTrue(self.preview.exists())

    def test_symlinks_are_not_followed_or_deleted(self):
        self.compressed.unlink()
        self.compressed.symlink_to(self.source)
        result = self.cleanup(apply=True)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.compressed.is_symlink())
        self.assertTrue(self.preview.exists())

    def test_filesystem_error_is_reported_without_deleting_preview(self):
        with patch.object(Path, 'unlink', side_effect=PermissionError('read only')):
            result = self.cleanup(apply=True)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['deleted_files'], 0)
        self.assertTrue(self.preview.exists())


@unittest.skipUnless(os.environ.get('CAMERATRAP_TEST_POSTGRES') == '1', 'Opt-in PostgreSQL tests')
class CleanupPostgresTests(unittest.TestCase):
    """Run actual queries against temporary tables and delete only temporary files."""

    def setUp(self):
        import test_archive_download as recovery_tests
        self.fixture = recovery_tests.DownloadQueuePostgresTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.conn = self.fixture.conn
        self.now = datetime.now(timezone.utc)
        self.conn.execute(text('ALTER TABLE media ADD COLUMN converted_file_name TEXT'))
        self.conn.execute(text('UPDATE media SET converted_file_name=:name'), {'name': self.fixture.output.name})
        self.conn.execute(text('UPDATE archive_transfers SET finished_at=:finished'),
                          {'finished': self.now - timedelta(days=3)})
        self.source = self.fixture.folder / 'sample.avi'
        self.source.write_bytes(b'original')
        self.preview = self.fixture.folder / 'sample.avi.preview-640.mp4'
        self.preview.write_bytes(b'preview')
        self.fixture.output.write_bytes(self.fixture.remote.read_bytes())

    def cleanup(self, apply=True, now=None):
        return media_cleanup.cleanup_local_videos(
            self.fixture.engine, self.fixture.folder, apply=apply, now=now or self.now,
            archive_retention=timedelta(hours=48), recovery_retention=timedelta(hours=24),
        )

    def test_only_saved_owned_and_verified_archives_are_eligible(self):
        self.assertEqual(self.cleanup(apply=False)['eligible_files'], 2)
        for statement in (
            "UPDATE video_conversions SET status='pending'",
            "UPDATE archive_transfers SET status='pending'",
            "UPDATE archive_transfers SET output_md5='different'",
            "UPDATE archive_transfers SET finished_at=NULL",
            "UPDATE sighting SET operator='other'",
            "UPDATE media SET converted_file_name=NULL",
            'DELETE FROM media',
        ):
            with self.subTest(statement=statement):
                nested = self.conn.begin_nested()
                self.conn.execute(text(statement))
                self.assertEqual(self.cleanup(apply=False)['eligible_files'], 0)
                nested.rollback()

    def test_cleanup_keeps_records_and_preview_and_allows_recovery(self):
        result = self.cleanup()
        self.assertEqual(result['deleted_files'], 2)
        self.assertTrue(self.preview.is_file())
        self.assertEqual(self.conn.execute(text('SELECT status FROM archive_transfers')).scalar(), 'completed')
        self.assertEqual(self.conn.execute(text('SELECT COUNT(*) FROM media')).scalar(), 1)
        with self.fixture.main.app.test_request_context():
            self.assertIn(self.preview.name, self.fixture.main.video_preview_url('sample.avi'))
        response = self.fixture.client.post(self.fixture.restore_url, headers=self.fixture.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.cleanup()['deleted_files'], 0)
        import archive_download
        with patch('archive_download.download_archive', side_effect=lambda remote, output, md5, **kw: output.write_bytes(self.fixture.remote.read_bytes())):
            archive_download.process_one_download(self.fixture.engine, self.fixture.folder)
        self.assertEqual(self.cleanup()['deleted_files'], 0)
        self.assertTrue(self.fixture.output.is_file())
        self.assertEqual(self.cleanup(now=self.now + timedelta(hours=25))['deleted_files'], 1)
        self.assertTrue(self.preview.is_file())

    def test_active_recovery_is_skipped_before_any_file_is_deleted(self):
        from archive_download import enqueue_download
        enqueue_download(self.conn, 'sample.avi')
        for state in ('pending', 'processing'):
            self.conn.execute(text('UPDATE archive_downloads SET status=:status'), {'status': state})
            result = self.cleanup()
            self.assertEqual(result['deleted_files'], 0)
            self.assertEqual(result['skipped_jobs'], 1)
            self.assertTrue(self.source.exists())
            self.assertTrue(self.fixture.output.exists())


class CleanupConfigTests(unittest.TestCase):
    def setUp(self):
        from scripts import cleanup_local_videos
        self.cli = cleanup_local_videos
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / '.env'

    def write_config(self, settings):
        fields = dict(db_url='sqlite://', upload_folder=self.directory.name, **settings)
        self.config.write_text('\n'.join(
            key if value is None else f'{key}={value}' for key, value in fields.items()
        ) + '\n')

    def test_config_file_is_reread_on_every_execution_and_accepts_fractional_hours(self):
        for archive, recovery in [('48', '24'), ('7.5', '0.25')]:
            with self.subTest(archive=archive, recovery=recovery):
                self.write_config(dict(ARCHIVE_RETENTION=archive, RECOVERY_RETENTION=recovery))
                with (
                    patch('sys.argv', ['cleanup', '--config', str(self.config)]),
                    patch.object(self.cli, 'create_engine') as create,
                    patch.object(self.cli, 'cleanup_local_videos', return_value={'errors': 0}) as cleanup,
                    patch.object(self.cli.logging, 'basicConfig'),
                ):
                    self.assertEqual(self.cli.main(), 0)
                    self.assertFalse(cleanup.call_args.kwargs['apply'])
                    self.assertEqual(cleanup.call_args.kwargs['archive_retention'], timedelta(hours=float(archive)))
                    self.assertEqual(cleanup.call_args.kwargs['recovery_retention'], timedelta(hours=float(recovery)))
                    create.return_value.dispose.assert_called_once()

    def test_missing_or_invalid_settings_abort_before_database_or_cleanup(self):
        for key in ('ARCHIVE_RETENTION', 'RECOVERY_RETENTION'):
            for value in ('missing', None, '', '0', '-1', 'nan', 'inf', '1e100', 'abc'):
                with self.subTest(key=key, value=value):
                    fields = dict(ARCHIVE_RETENTION='48', RECOVERY_RETENTION='24')
                    if value == 'missing':
                        del fields[key]
                    else:
                        fields[key] = value
                    self.write_config(fields)
                    error = io.StringIO()
                    with (
                        patch('sys.argv', ['cleanup', '--config', str(self.config), '--apply']),
                        patch.object(self.cli, 'create_engine') as create,
                        patch.object(self.cli, 'cleanup_local_videos') as cleanup,
                        patch.object(self.cli.logging, 'basicConfig'),
                        redirect_stderr(error),
                    ):
                        with self.assertRaises(SystemExit) as stopped:
                            self.cli.main()
                        self.assertEqual(stopped.exception.code, 2)
                        self.assertIn(key, error.getvalue())
                        create.assert_not_called()
                        cleanup.assert_not_called()
