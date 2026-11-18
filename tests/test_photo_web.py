"""Exercise photo upload, signed ownership, atomic saving and photo display."""

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy import text

import test_admin_visibility as visibility
from test_sighting_edit import Form
from photo_media import md5_file


class PhotoWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        visibility.AdminVisibilityTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        visibility.AdminVisibilityTests.tearDownClass()

    def setUp(self):
        self.fixture = visibility.AdminVisibilityTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.main, self.client, self.db = self.fixture.main, self.fixture.client, self.fixture.database
        self.fixture.login('alice')
        self.prefix = self.fixture.prefix
        with self.db.begin() as conn:
            conn.execute(text('ALTER TABLE sighting ADD COLUMN institution TEXT'))
            for field in ('original_file_name', 'file_content_md5', 'converted_file_name'):
                conn.execute(text(f'ALTER TABLE media ADD COLUMN {field} TEXT'))
            conn.execute(text('CREATE TABLE photo_archive_transfers (media_id INTEGER PRIMARY KEY, source_filename TEXT UNIQUE, source_md5 TEXT, destination TEXT, status TEXT DEFAULT \'pending\', attempts INTEGER DEFAULT 0)'))
            conn.execute(text('CREATE TABLE photo_archive_downloads (media_id INTEGER PRIMARY KEY, status TEXT DEFAULT \'pending\', attempts INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT, last_error TEXT, next_attempt_at TEXT)'))
        for patcher in (
            patch.dict(self.main.config, archive_storage='OF2/test_photos'),
            patch.object(self.main.camtrap_banner_decoder, 'extract_date_time', return_value={'date': '2026-11-18', 'time': '123456'}),
            patch.object(self.main, 'extract_frame'),
            patch.object(self.main.app.logger, 'disabled', True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def image_bytes(self, format='JPEG', exif=False):
        data = io.BytesIO()
        tags = Image.Exif()
        if exif:
            tags[0x8769] = {36867: '2026:11:17 09:10:11'}
        Image.new('RGB', (1600, 800), 'white').save(data, format, exif=tags)
        return data.getvalue()

    def upload(self, content=None, filename='camera.jpg'):
        response = self.client.get(self.prefix + '/upload_camtrap_photo_form')
        self.assertEqual(response.status_code, 200)
        token = Form(response.get_data(as_text=True)).fields['csrf_token']['value']
        return self.client.post(self.prefix + '/upload_camtrap_photo', data={
            'csrf_token': token, 'photo': (io.BytesIO(content or self.image_bytes()), filename),
        }, content_type='multipart/form-data')

    def data(self, html, **changes):
        form = Form(html)
        data = {name: field.get('value', '') for name, field in form.fields.items()}
        data.update(code='PHOTO-TEST', camtrap_id='FT01', wolf_number='1', scalp='C1', note='Photo notes', latitude='45', longitude='7')
        data.update(changes)
        return data

    def rows(self, table):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(text(f'SELECT * FROM {table}')).mappings()]

    def test_upload_proposes_exif_date_and_creates_preview_without_archiving(self):
        source = self.image_bytes(exif=True)
        response = self.upload(source)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        form = Form(html)
        self.assertEqual(form.fields['date']['value'], '2026-11-17')
        self.assertEqual(form.fields['time_']['value'], '09:10:11')
        self.assertIn('EXIF originale', html)
        self.assertIn('Banda OCR', html)
        self.assertIn('photo-date-choice', html)
        self.assertNotIn('<video', html)
        original = self.fixture.folder / form.fields['new_file_name']['value']
        self.assertEqual(original.read_bytes(), source)
        self.assertTrue(original.with_name(original.name + '.preview-1280.jpg').is_file())
        self.assertEqual(self.rows('photo_archive_transfers'), [])
        self.main.start_conversion_worker.assert_not_called()

    def test_save_is_atomic_and_archives_original_without_video_conversion(self):
        response = self.upload()
        html = response.get_data(as_text=True)
        payload = self.data(html, operator='bob', file_content_md5='forged', date='2026-11-19', time_='10:20')
        source_name = payload['new_file_name']
        source = self.fixture.folder / source_name
        checksum = md5_file(source)
        response = self.client.post(self.prefix + '/save_info', data=payload)
        self.assertEqual(response.status_code, 302)
        media = next(row for row in self.rows('media') if row['media_type']=='photo')
        sighting = next(row for row in self.rows('sighting') if row['id']==media['sighting_id'])
        job = self.rows('photo_archive_transfers')[0]
        self.assertEqual(sighting['operator'], 'alice')
        self.assertEqual((sighting['latitude'], sighting['longitude']), (45, 7))
        self.assertEqual(media['file_content_md5'], checksum)
        self.assertIsNone(media['converted_file_name'])
        self.assertTrue(media['image'])
        self.assertEqual(json.loads(media['capture_metadata'])['initial_selection'], 'manual')
        self.assertEqual(job['source_md5'], checksum)
        self.assertEqual(job['destination'], 'OF2:test_photos/' + source_name)
        self.assertEqual(source.read_bytes(), self.image_bytes())
        self.main.extract_frame.assert_not_called()
        self.main.start_conversion_worker.assert_called_once()
        for path in (response.location, self.prefix + '/sighting_list', self.prefix + '/edit_sighting/' + str(sighting['id'])):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('<video', page.get_data(as_text=True))
            self.assertIn('.preview-1280.jpg', page.get_data(as_text=True))
        # A signed upload cannot be registered twice.
        self.assertEqual(self.client.post(self.prefix + '/save_info', data=payload).status_code, 400)
        self.assertEqual(len(self.rows('photo_archive_transfers')), 1)

    def test_form_error_keeps_photo_and_draft_without_writes(self):
        html = self.upload().get_data(as_text=True)
        payload = self.data(html, latitude='nan', longitude='7', note='<draft>')
        before = len(self.rows('sighting'))
        response = self.client.post(self.prefix + '/save_info', data=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.rows('sighting')), before)
        self.assertIn('&lt;draft&gt;', response.get_data(as_text=True))
        self.assertIn('photo_upload_token', response.get_data(as_text=True))
        self.assertEqual(self.rows('photo_archive_transfers'), [])

    def test_signed_upload_cannot_be_stolen_or_tampered_with(self):
        html = self.upload().get_data(as_text=True)
        payload = self.data(html)
        self.fixture.login('bob')
        self.assertEqual(self.client.post(self.prefix + '/save_info', data=payload).status_code, 403)
        self.fixture.login('alice')
        payload['photo_upload_token'] += 'bad'
        self.assertEqual(self.client.post(self.prefix + '/save_info', data=payload).status_code, 400)
        self.assertEqual(self.rows('photo_archive_transfers'), [])

    def test_original_mutation_is_detected_before_saving(self):
        payload = self.data(self.upload().get_data(as_text=True))
        (self.fixture.folder / payload['new_file_name']).write_bytes(b'changed')
        self.assertEqual(self.client.post(self.prefix + '/save_info', data=payload).status_code, 409)
        self.assertEqual(self.rows('photo_archive_transfers'), [])

    def test_invalid_image_is_rejected_and_staged_file_is_removed(self):
        response = self.upload(b'not a jpeg')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.fixture.folder.glob('*.jpg')), [])

    def test_archive_status_restricts_owner_and_preserves_preview_after_local_deletion(self):
        payload = self.data(self.upload().get_data(as_text=True))
        self.client.post(self.prefix + '/save_info', data=payload)
        media = next(row for row in self.rows('media') if row['media_type']=='photo')
        url = self.prefix + '/photo_archive_status/' + str(media['id'])
        status = self.client.get(url)
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json['download_url'].endswith(payload['new_file_name']))
        with self.db.begin() as conn:
            conn.execute(text("UPDATE photo_archive_transfers SET status='completed'"))
        (self.fixture.folder / payload['new_file_name']).unlink()
        status = self.client.get(url)
        self.assertIsNone(status.json['download_url'])
        self.assertTrue(status.json['restore_url'])
        page = self.client.get(self.prefix + '/view/' + str(media['sighting_id'])).get_data(as_text=True)
        self.assertIn('.preview-1280.jpg', page)
        self.fixture.login('bob')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.fixture.login('admin')
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_upload_requires_login_and_csrf(self):
        self.assertEqual(self.main.app.test_client().get(self.prefix + '/upload_camtrap_photo_form').status_code, 302)
        self.assertEqual(self.client.post(self.prefix + '/upload_camtrap_photo').status_code, 400)

    def test_png_original_is_saved_and_queued_without_reencoding(self):
        original = self.image_bytes(format='PNG')
        response = self.upload(original, 'camera.png')
        payload = self.data(response.get_data(as_text=True))
        self.assertEqual(self.client.post(self.prefix + '/save_info', data=payload).status_code, 302)
        row = self.rows('photo_archive_transfers')[0]
        self.assertTrue(row['source_filename'].endswith('.png'))
        self.assertEqual((self.fixture.folder / row['source_filename']).read_bytes(), original)

    def test_editing_photo_metadata_does_not_requeue_archiving(self):
        payload = self.data(self.upload().get_data(as_text=True))
        self.client.post(self.prefix + '/save_info', data=payload)
        media = next(row for row in self.rows('media') if row['media_type']=='photo')
        jobs = self.rows('photo_archive_transfers')
        self.main.start_conversion_worker.reset_mock()
        self.fixture.login('admin')
        path = self.prefix + '/edit_sighting/' + str(media['sighting_id'])
        response = self.client.get(path)
        form = Form(response.get_data(as_text=True))
        fields = {name: field.get('value','') for name,field in form.fields.items()}
        fields.update(code='PHOTO-EDITED', wolf_number='2', scalp='C3', camtrap_id='FT01', note='Edited by admin')
        (self.fixture.folder / media['new_file_name']).unlink()
        self.assertEqual(self.client.post(path, data=fields).status_code, 302)
        self.assertEqual(self.rows('photo_archive_transfers'), jobs)
        self.main.start_conversion_worker.assert_not_called()
