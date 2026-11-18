"""Check EXIF/OCR date priority, oriented previews and original preservation."""
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import camtrap_banner_decoder
from photo_media import capture_candidate, md5_file, prepare_photo, thumbnail_bytes


class PhotoMediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def photo(self, name='sample.jpg', dates=None, orientation=None, size=(2400, 1200)):
        path = self.folder / name
        exif = Image.Exif()
        if dates:
            exif[0x8769] = dates
        if orientation:
            exif[274] = orientation
        Image.new('RGB', size, 'white').save(path, exif=exif)
        return path

    def prepare(self, path, ocr=None):
        with patch('photo_media.camtrap_banner_decoder.extract_date_time', return_value=ocr or {'date': '', 'time': ''}) as decode:
            result = prepare_photo(path, 'Europe/Rome', 5)
            self.assertEqual(decode.call_args.args[1].ocr_timeout, 5)
            return result

    def test_original_exif_precedes_creation_and_ocr_and_reports_conflicts(self):
        path = self.photo(dates={36867: '2026:07:01 12:34:56', 36881: '+02:00', 36868: '2026:07:02 15:00:00'})
        original = md5_file(path)
        data = self.prepare(path, {'date': '2026-07-03', 'time': '160000'})
        self.assertEqual(data['date'], '2026-07-01')
        self.assertEqual(data['time_'], '12:34:56')
        self.assertEqual([item['source'] for item in data['capture_metadata']['candidates']], ['exif_original', 'exif_creation', 'ocr'])
        self.assertTrue(data['capture_metadata']['conflict'])
        self.assertEqual(md5_file(path), original)
        with Image.open(path.with_name(data['preview_file_name'])) as preview:
            self.assertEqual(preview.size, (1280, 640))
        self.assertTrue(thumbnail_bytes(path.with_name(data['preview_file_name'])))

    def test_creation_fallback_and_offset_conversion(self):
        path = self.photo(dates={36867: 'invalid', 36868: '2026:01:01 12:00:00', 36882: '+00:00'})
        data = self.prepare(path)
        self.assertEqual(data['time_'], '13:00:00')
        self.assertEqual(data['capture_metadata']['initial_source'], 'exif_creation')

    def test_ocr_fallback_for_png_and_unknown_dates(self):
        path = self.photo('sample.png')
        data = self.prepare(path, {'date': '2026-11-18', 'time': '123456'})
        self.assertEqual((data['date'], data['time_']), ('2026-11-18', '12:34:56'))
        self.assertEqual(data['capture_metadata']['initial_source'], 'ocr')
        empty = self.prepare(path)
        self.assertEqual((empty['date'], empty['time_']), ('', ''))

    def test_preview_orientation_does_not_change_original(self):
        path = self.photo(orientation=6, size=(400, 200))
        digest = md5_file(path)
        data = self.prepare(path)
        with Image.open(path.with_name(data['preview_file_name'])) as preview:
            self.assertEqual(preview.size, (200, 400))
        self.assertEqual(md5_file(path), digest)

    def test_ocr_failure_does_not_discard_valid_exif(self):
        path = self.photo(dates={36867: '2026:11:18 10:00:00'})
        with patch('photo_media.camtrap_banner_decoder.extract_date_time', side_effect=RuntimeError('OCR timeout')):
            data = prepare_photo(path, 'Europe/Rome')
        self.assertEqual(data['date'], '2026-11-18')
        self.assertTrue(data['capture_metadata']['warnings'])

    def test_invalid_images_and_mismatched_extensions_are_rejected(self):
        bad = self.folder / 'bad.jpg'
        bad.write_bytes(b'not an image')
        with self.assertRaises(OSError):
            prepare_photo(bad, 'Europe/Rome')
        Image.new('RGB', (10, 10)).save(bad, 'PNG')
        with self.assertRaises(ValueError):
            prepare_photo(bad, 'Europe/Rome')

    def test_ocr_timeout_is_forwarded_without_system_exit(self):
        args = Namespace(debug=False, ocr_timeout=0.1)
        with patch.object(camtrap_banner_decoder.pytesseract, 'image_to_string', side_effect=RuntimeError('timeout')) as ocr:
            result = camtrap_banner_decoder.banner_text_from_frame(np.zeros((100, 200, 3), dtype=np.uint8), args)
        self.assertIsNone(result)
        self.assertEqual(args.ocr_error, 'timeout')
        self.assertEqual(ocr.call_args.kwargs['timeout'], 0.1)

    def test_invalid_dates_and_nonexistent_local_times_are_ignored(self):
        for date in ('2026:02:30 12:00:00', '2026:03:29 02:30:00', 'garbage'):
            self.assertIsNone(capture_candidate(date, None, 'exif_original', 'EXIF', 'Europe/Rome'))
