"""Verify executable configuration reaches Flask OCR and background conversion."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values

import camtrap_banner_decoder
import video_conversion
from scripts import run_video_conversions


class ExecutablePathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def test_flask_reads_configured_paths_and_keeps_path_fallbacks(self):
        root = Path(__file__).resolve().parent.parent
        original = dict(dotenv_values(root / '.env'))
        for settings, expected in [
            ({}, ('tesseract', 'ffmpeg')),
            (dict(TESSERACT_CMD=' ', FFMPEG_CMD=None), ('tesseract', 'ffmpeg')),
            (dict(TESSERACT_CMD='/opt/OCR tools/tesseract', FFMPEG_CMD='/opt/media tools/ffmpeg'),
             ('/opt/OCR tools/tesseract', '/opt/media tools/ffmpeg')),
        ]:
            with self.subTest(settings=settings):
                config = {key: value for key, value in original.items() if key not in ('TESSERACT_CMD', 'FFMPEG_CMD')}
                config.update(settings, log_file=str(self.folder / 'app.log'))
                name = '_executable_path_test_app'
                spec = importlib.util.spec_from_file_location(name, root / 'main.py')
                module = importlib.util.module_from_spec(spec)
                with (
                    patch.dict(sys.modules, {name: module}),
                    patch('dotenv.dotenv_values', return_value=config),
                    patch.object(camtrap_banner_decoder.pytesseract.pytesseract, 'tesseract_cmd'),
                ):
                    try:
                        spec.loader.exec_module(module)
                        self.assertEqual(module.app.config['TESSERACT_CMD'], expected[0])
                        self.assertEqual(module.app.config['FFMPEG_CMD'], expected[1])
                        self.assertEqual(camtrap_banner_decoder.pytesseract.pytesseract.tesseract_cmd, expected[0])
                    finally:
                        if hasattr(module, 'engine'):
                            module.engine.dispose()
                        if hasattr(module, 'app'):
                            for handler in list(module.app.logger.handlers):
                                if getattr(handler, 'baseFilename', None) == config['log_file']:
                                    module.app.logger.removeHandler(handler)
                                    handler.close()

    def test_worker_entry_point_reads_ffmpeg_from_env(self):
        for value, expected in [('/opt/media tools/ffmpeg', '/opt/media tools/ffmpeg'), (' ', 'ffmpeg'), (None, 'ffmpeg')]:
            with self.subTest(value=value):
                config = dict(db_url='sqlite://', upload_folder=str(self.folder), FFMPEG_CMD=value)
                with (
                    patch('sys.argv', ['run_video_conversions']),
                    patch.object(run_video_conversions, 'dotenv_values', return_value=config),
                    patch.object(run_video_conversions, 'create_engine'),
                    patch.object(run_video_conversions, 'run_worker') as run,
                    patch.object(run_video_conversions.signal, 'signal'),
                    patch.object(run_video_conversions.logging, 'basicConfig'),
                ):
                    run_video_conversions.main()
                    self.assertEqual(run.call_args.kwargs['ffmpeg_binary'], expected)

    def test_queue_processor_passes_ffmpeg_path_to_encoder(self):
        source = self.folder / 'sample.avi'
        source.write_bytes(b'original')
        job = dict(source_filename=source.name, source_md5=video_conversion.file_md5(source),
                   output_filename=source.name + '.compressed.mp4', attempts=0)
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.fetchone.return_value = job
        engine = MagicMock()
        engine.begin.return_value.__enter__.return_value = connection
        with patch.object(video_conversion, 'convert_video', side_effect=lambda source, output, **kw: output.write_bytes(b'MP4')) as encode:
            self.assertTrue(video_conversion.process_one(engine, self.folder, ffmpeg_binary='/opt/media tools/ffmpeg'))
            self.assertEqual(encode.call_args.kwargs['ffmpeg_binary'], '/opt/media tools/ffmpeg')

    def test_shared_worker_passes_ffmpeg_path_to_queue_processor(self):
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value.execute.return_value.scalar.return_value = True
        with (
            patch.object(video_conversion, 'process_one', return_value=False) as process,
            patch('archive_download.process_one_download', return_value=False),
            patch('archive_download.recover_downloads'),
        ):
            video_conversion.run_worker(engine, self.folder, ffmpeg_binary='/opt/media tools/ffmpeg')
            self.assertEqual(process.call_args.kwargs['ffmpeg_binary'], '/opt/media tools/ffmpeg')
