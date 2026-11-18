"""Verify sighting edits, administrator access and preservation of media and archives."""

import unittest
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text

import test_admin_visibility as visibility


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.selects = {}
        self.select = None
        self.notes = ''
        self.in_notes = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            if attrs['name'] in self.fields:
                raise AssertionError('Duplicate field: ' + attrs['name'])
            self.fields[attrs['name']] = attrs
        elif tag == 'select':
            self.select = attrs.get('name')
            self.selects[self.select] = []
        elif tag == 'option' and self.select:
            self.selects[self.select].append(attrs)
        elif tag == 'textarea' and attrs.get('name') == 'note':
            self.in_notes = True

    def handle_endtag(self, tag):
        if tag == 'select':
            self.select = None
        elif tag == 'textarea':
            self.in_notes = False

    def handle_data(self, data):
        if self.in_notes:
            self.notes += data


class SightingEditTests(unittest.TestCase):
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
        self.main = self.fixture.main
        self.client = self.fixture.client
        self.database = self.fixture.database
        self.fixture.login('alice')
        self.path = self.fixture.prefix + '/edit_sighting/1'
        with self.database.begin() as conn:
            conn.execute(text("ALTER TABLE sighting ADD COLUMN institution TEXT DEFAULT 'Original institution'"))
            conn.execute(text("ALTER TABLE sighting ADD COLUMN transect_id TEXT DEFAULT 'TR01'"))
            conn.execute(text("INSERT INTO fototrappole SELECT 'FT02', operator, tipo, data_inizio, data_fine, nome, cognome, regione, provincia, comune, location, country, 44, 8, altitudine, intersezioni FROM fototrappole WHERE operator='alice'"))
            conn.execute(text("UPDATE media SET image=:image WHERE sighting_id=1"), {'image': b'original image'})
            for table in ('video_conversions', 'archive_transfers', 'archive_downloads'):
                conn.execute(text(f'CREATE TABLE {table} (source_filename TEXT PRIMARY KEY, status TEXT)'))
                conn.execute(text(f"INSERT INTO {table} VALUES ('alice.mp4', 'completed')"))
        for patcher in (
            patch.object(self.main, 'extract_frame'),
            patch.object(self.main.camtrap_banner_decoder, 'extract_date_time'),
            patch.object(self.main.subprocess, 'run'),
            patch.object(self.main.app.logger, 'disabled', True),
            patch.dict(self.main.app.config, SIGHTING_TIMEZONE='Europe/Rome'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self):
        with self.database.connect() as conn:
            return dict(conn.execute(text('SELECT * FROM sighting WHERE id=1')).mappings().one())

    def media_state(self):
        with self.database.connect() as conn:
            return {
                table: [dict(row) for row in conn.execute(text(f'SELECT * FROM {table}')).mappings()]
                for table in ('media', 'video_conversions', 'archive_transfers', 'archive_downloads')
            }

    def data(self, **changes):
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        token = Form(response.get_data(as_text=True)).fields['csrf_token']['value']
        data = dict(csrf_token=token, code='UPDATED', camtrap_id='FT02', wolf_number='0',
                    scalp='C3', latitude='0', longitude='0', date='2026-11-18', time_='12:34',
                    note='Edited notes')
        data.update(changes)
        return data

    def test_owner_sees_edit_link_and_prefilled_form_without_local_original(self):
        html = self.client.get(self.fixture.prefix + '/view/1').get_data(as_text=True)
        self.assertIn(f'href="{self.path}"', html)
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        form = Form(html)
        self.assertEqual(form.fields['code']['value'], 'alice')
        self.assertEqual(form.fields['date']['value'], '2026-11-16')
        self.assertEqual(form.fields['time_']['value'], '13:00:00')
        self.assertEqual(form.fields['latitude']['value'], '45.0')
        self.assertEqual(form.fields['date']['form'], 'uploadForm')
        self.assertEqual(form.notes, 'alice')
        self.assertNotIn('new_file_name', form.fields)
        self.assertNotIn('file_content_md5', form.fields)
        self.assertIn('data:image/jpeg;base64,', html)
        self.assertNotIn('Scarica il\n                video originale', html)
        self.assertIn('get_fototrappola_data?sighting_id=1', html)
        self.assertEqual([item['value'] for item in form.selects['camtrap_id']], ['', 'FT01', 'FT02'])

    def test_owner_updates_metadata_without_changing_owner_media_or_jobs(self):
        before = self.row()
        media = self.media_state()
        response = self.client.post(self.path, data=self.data(
            operator='bob', institution='Forged', transect_id='Forged',
            new_file_name='other.mp4', file_content_md5='other',
        ))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/view/1'))
        row = self.row()
        for key in ('operator', 'institution', 'transect_id'):
            self.assertEqual(row[key], before[key])
        self.assertEqual((row['latitude'], row['longitude']), (0, 0))
        self.assertEqual(row['wolf_number'], 0)
        self.assertEqual(row['code'], 'UPDATED')
        self.assertEqual(row['camtrap_id'], 'FT02')
        self.assertEqual(row['scalp'], 'C3')
        self.assertEqual(row['timestamp'].utcoffset(), timedelta(hours=1))
        self.assertEqual(row['notes'], 'Edited notes')
        self.assertEqual(self.media_state(), media)
        self.main.start_conversion_worker.assert_not_called()
        self.main.extract_frame.assert_not_called()
        self.main.camtrap_banner_decoder.extract_date_time.assert_not_called()
        self.main.subprocess.run.assert_not_called()

    def test_administrator_edits_another_owners_sighting_with_owner_cameras(self):
        self.fixture.login('admin')
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        form = Form(response.get_data(as_text=True))
        self.assertEqual([item['value'] for item in form.selects['camtrap_id']], ['', 'FT01', 'FT02'])
        self.assertEqual(form.fields['operator']['value'], 'Alice')
        data = self.data()
        response = self.client.post(self.path + '?operator=alice&camtrap_id=FT01', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response.location).query), {'operator': ['alice'], 'camtrap_id': ['FT01']})
        self.assertEqual(self.row()['operator'], 'alice')
        bad = self.client.post(self.path, data=self.data(camtrap_id='ADM01'))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.row()['camtrap_id'], 'FT02')

    def test_coordinate_lookup_uses_sighting_owner_for_admin_and_rejects_other_users(self):
        url = self.fixture.prefix + '/get_fototrappola_data?sighting_id=1&camtrap_id=FT01'
        self.fixture.login('admin')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-latitude="45.0"', response.get_data(as_text=True))
        self.fixture.login('bob')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.fixture.login('alice')
        self.assertEqual(self.client.get(url + '&operator=bob').status_code, 200)
        self.assertEqual(self.client.get(url.replace('sighting_id=1', 'sighting_id=bad')).status_code, 400)

    def test_other_user_and_revoked_admin_cannot_edit(self):
        data = self.data()
        self.fixture.login('bob')
        with self.client.session_transaction() as session:
            session['sighting_csrf_token'] = data['csrf_token']
        self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertEqual(self.client.post(self.path, data=data).status_code, 404)
        self.fixture.login('admin')
        data = self.data()
        self.fixture.accounts['admin']['is_admin'] = False
        self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertEqual(self.client.post(self.path, data=data).status_code, 404)
        self.assertEqual(self.row()['code'], 'alice')

    def test_invalid_fields_preserve_draft_and_do_not_write(self):
        before = self.row()
        for changes in (
            dict(code='bob'), dict(code=''), dict(camtrap_id='ADM01'),
            dict(wolf_number='-1'), dict(wolf_number='121'), dict(wolf_number='1.5'),
            dict(scalp='INVALID'), dict(latitude='91'), dict(longitude='nan'),
            dict(latitude='', longitude='7'), dict(date='2026-02-30'),
            dict(date='2026-11-18', time_=''), dict(date='2026-03-29', time_='02:30'),
        ):
            with self.subTest(changes=changes):
                data = self.data(note='<draft>\nKeep this', **changes)
                response = self.client.post(self.path, data=data)
                self.assertEqual(response.status_code, 400)
                html = response.get_data(as_text=True)
                self.assertIn('&lt;draft&gt;', html)
                self.assertEqual(Form(html).notes, '<draft>\nKeep this')
                self.assertEqual(Form(html).fields['code']['value'], data['code'])
                self.assertEqual(self.row(), before)

    def test_unknown_timestamp_and_coordinates_can_remain_empty(self):
        response = self.client.post(self.path, data=self.data(code='alice', date='', time_='', latitude='', longitude='', camtrap_id=''))
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertIsNone(row['timestamp'])
        self.assertIsNone(row['latitude'])
        self.assertIsNone(row['longitude'])
        self.assertIsNone(row['camtrap_id'])

    def test_stored_snapshot_prefills_instead_of_current_camera_coordinates(self):
        with self.database.begin() as conn:
            conn.execute(text('UPDATE sighting SET latitude=44.1, longitude=8.2 WHERE id=1'))
        html = self.client.get(self.path).get_data(as_text=True)
        form = Form(html)
        self.assertEqual(form.fields['latitude']['value'], '44.1')
        self.assertEqual(form.fields['longitude']['value'], '8.2')
        self.assertIn('data-preserve-coordinates="true"', html)

    def test_retained_video_preview_is_used_without_requiring_original(self):
        preview = self.fixture.folder / 'alice.mp4.preview-640.mp4'
        preview.write_bytes(b'cached preview')
        html = self.client.get(self.path).get_data(as_text=True)
        self.assertIn('alice.mp4.preview-640.mp4', html)
        self.assertIn('<video id="video_camtrap"', html)
        html = self.client.get(self.fixture.prefix + '/view/1').get_data(as_text=True)
        self.assertIn('<video class="sighting-media"', html)
        self.assertIn('alice.mp4.preview-640.mp4', html)
        self.assertIn('controls playsinline preload="none"', html)
        self.assertIn('poster="data:image/jpeg;base64,', html)
        self.assertNotIn('Anteprima video non disponibile', html)
        self.main.subprocess.run.assert_not_called()

    def test_metadata_without_media_can_be_viewed_and_edited(self):
        with self.database.begin() as conn:
            conn.execute(text('DELETE FROM media WHERE sighting_id=1'))
        html = self.client.get(self.path).get_data(as_text=True)
        self.assertEqual(Form(html).fields['date']['value'], '2026-11-16')
        response = self.client.post(self.path, data=self.data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(response.location).status_code, 200)
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn('Anteprima video non disponibile', html)
        self.assertIn('Immagine non disponibile', html)
        self.assertNotIn('<video', html)

    def test_legacy_scalp_value_can_be_preserved(self):
        with self.database.begin() as conn:
            conn.execute(text("UPDATE sighting SET scalp='C2' WHERE id=1"))
        form = Form(self.client.get(self.path).get_data(as_text=True))
        self.assertIn({'value': 'C2', 'selected': None}, form.selects['scalp'])
        self.assertEqual(self.client.post(self.path, data=self.data(scalp='C2')).status_code, 302)

    def test_csrf_and_authentication_are_required(self):
        before = self.row()
        for token in ('', 'wrong', 'é'):
            response = self.client.post(self.path, data=self.data(csrf_token=token))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.row(), before)
        anonymous = self.main.app.test_client()
        self.assertEqual(anonymous.get(self.path).status_code, 302)
        self.assertEqual(anonymous.post(self.path).status_code, 302)
        self.assertEqual(self.client.get(self.fixture.prefix + '/edit_sighting/999').status_code, 404)
