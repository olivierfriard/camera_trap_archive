"""Verify WebP output, exact metadata preservation and atomic failure handling."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from photo_conversion import convert_photo, read_preserved_metadata, source_metadata
from photo_media import md5_file


class PhotoConversionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.target = self.folder / 'photo.webp'

    def test_jpeg_exif_gps_orientation_icc_xmp_and_unknown_segments(self):
        source = self.folder / 'source.jpg'
        exif = Image.Exif()
        exif[274] = 6
        exif[0x8769] = {36867: '2026:11:18 12:34:56', 37500: b'private maker data'}
        exif[0x8825] = {1: 'N', 2: (45, 1, 2), 3: 'E', 4: (7, 3, 4)}
        Image.new('RGB', (80, 40), 'green').save(source, 'JPEG', exif=exif,
            icc_profile=b'example ICC bytes', xmp=b'<x:xmpmeta>test</x:xmpmeta>')
        data = source.read_bytes()
        # Include arbitrary legacy metadata unsupported by native WebP readers.
        source.write_bytes(data[:2] + b'\xff\xed\x00\x0aIPTCtest' + b'\xff\xfe\x00\x09comment' + data[2:])
        before = source.read_bytes()
        digest = convert_photo(source, self.target, md5_file(source))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(md5_file(self.target), digest)
        self.assertEqual(read_preserved_metadata(self.target), source_metadata(source))
        with Image.open(source) as original, Image.open(self.target) as converted:
            self.assertEqual(converted.format, 'WEBP')
            self.assertEqual(converted.size, (80, 40))
            self.assertEqual(converted.getexif()[274], 6)
            self.assertEqual(converted.getexif().get_ifd(0x8825), original.getexif().get_ifd(0x8825))
            for key in ('exif', 'icc_profile', 'xmp'):
                actual, expected = converted.info[key], original.info[key]
                if key == 'exif':
                    actual, expected = actual.removeprefix(b'Exif\0\0'), expected.removeprefix(b'Exif\0\0')
                self.assertEqual(actual, expected)

    def test_png_text_unicode_exif_transparency_and_unknown_chunks(self):
        source = self.folder / 'source.png'
        info = PngImagePlugin.PngInfo()
        info.add_text('Comment', 'Camera trap')
        info.add_itxt('Description', 'Lupo à Torino')
        info.add_itxt('XML:com.adobe.xmp', '<x:xmpmeta>test</x:xmpmeta>')
        info.add(b'prIv', b'unknown metadata')
        exif = Image.Exif()
        exif[306] = '2026:11:18 12:00:00'
        Image.new('RGBA', (50, 30), (20, 40, 80, 100)).save(source, 'PNG', pnginfo=info, exif=exif)
        before = source.read_bytes()
        convert_photo(source, self.target, md5_file(source))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(read_preserved_metadata(self.target), source_metadata(source))
        self.assertIn('prIv', [tag for tag, _ in read_preserved_metadata(self.target)['blocks']])
        with Image.open(self.target) as converted:
            self.assertEqual(converted.getexif()[306], exif[306])
            self.assertEqual(converted.getpixel((0, 0))[3], 100)

    def test_failed_verification_does_not_publish_or_change_original(self):
        source = self.folder / 'source.png'
        Image.new('RGB', (40, 40)).save(source)
        before = source.read_bytes()
        with patch('photo_conversion.read_preserved_metadata', return_value={}):
            with self.assertRaisesRegex(ValueError, 'metadata verification'):
                convert_photo(source, self.target, md5_file(source))
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.folder.glob('.photo-webp-*')), [])

    def test_changed_original_and_unsupported_modes_are_retained(self):
        source = self.folder / 'source.jpg'
        Image.new('CMYK', (40, 40)).save(source)
        with self.assertRaisesRegex(ValueError, 'registered hash'):
            convert_photo(source, self.target, 'wrong')
        with self.assertRaisesRegex(ValueError, 'mode'):
            convert_photo(source, self.target, md5_file(source))
        self.assertTrue(source.exists())
        self.assertFalse(self.target.exists())

    def test_progressive_jpeg_metadata_is_preserved(self):
        source = self.folder / 'source.jpg'
        Image.new('RGB', (80, 40), 'blue').save(source, progressive=True)
        convert_photo(source, self.target, md5_file(source))
        self.assertEqual(read_preserved_metadata(self.target), source_metadata(source))
        self.assertEqual(self.target.read_bytes()[12:16], b'VP8X')


if __name__ == '__main__':
    unittest.main()
