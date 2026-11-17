"""Exercise administrator list scopes, read-only inspection and owner isolation."""

import csv
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from sqlalchemy import create_engine, text


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = [dict(attrs).get('href', ''), '']

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None


class AdminVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory()
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / '.env'))
        config['log_file'] = str(Path(cls.logs.name) / 'app.log')
        with patch('dotenv.dotenv_values', return_value=config):
            import main
        cls.main = main
        sqlite3.register_converter('ADMIN_TEST_TIMESTAMP', lambda value: datetime.fromisoformat(value.decode()))

    @classmethod
    def tearDownClass(cls):
        cls.logs.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.database = create_engine('sqlite://', connect_args={'detect_types': sqlite3.PARSE_DECLTYPES})
        self.addCleanup(self.database.dispose)
        self.accounts = {
            username: dict(username=username, fullname=fullname, institution='Test', code='T',
                           is_active=active, is_admin=username == 'admin')
            for username, fullname, active in [
                ('admin', 'Admin', True), ('alice', 'Alice', True),
                ('bob', 'Bob <B>', True), ('inactive', 'Former user', False),
            ]
        }
        with self.database.begin() as conn:
            conn.execute(text("ATTACH DATABASE ':memory:' AS public"))
            conn.execute(text('CREATE TABLE public.users (username TEXT PRIMARY KEY, fullname TEXT, is_active BOOLEAN)'))
            conn.execute(text('INSERT INTO public.users VALUES (:username, :fullname, :is_active)'), list(self.accounts.values()))
            conn.execute(text('''CREATE TABLE fototrappole (
                codice TEXT, operator TEXT, tipo TEXT, data_inizio DATE, data_fine DATE,
                nome TEXT, cognome TEXT, regione TEXT, provincia TEXT, comune TEXT,
                location TEXT, country TEXT, latitudine REAL, longitudine REAL,
                altitudine REAL, intersezioni TEXT
            )'''))
            conn.execute(text('''CREATE TABLE sighting (
                id INTEGER PRIMARY KEY, code TEXT, timestamp ADMIN_TEST_TIMESTAMP, operator TEXT,
                camtrap_id TEXT, wolf_number INTEGER, scalp TEXT, latitude REAL, longitude REAL, notes TEXT
            )'''))
            conn.execute(text('CREATE TABLE media (id INTEGER PRIMARY KEY, sighting_id INTEGER, image BLOB, new_file_name TEXT)'))
            for id, (operator, code, latitude) in enumerate([
                ('alice', 'FT01', 45), ('bob', 'FT01', 46),
                ('admin', 'ADM01', 47), ('inactive', 'OLD01', 48),
            ], start=1):
                params = dict(id=id, operator=operator, code=code, latitude=latitude)
                conn.execute(text('''INSERT INTO fototrappole VALUES
                    (:code, :operator, :operator, '2026-11-01', NULL, '', '',
                     'Piemonte', 'Torino', 'Torino', 'Bosco', 'Italia', :latitude, 7, 300, '')
                '''), params)
                conn.execute(text('''INSERT INTO sighting VALUES
                    (:id, :operator, '2026-11-16T12:00:00+00:00', :operator,
                     :code, 1, 'C1', NULL, NULL, :operator)
                '''), params)
                conn.execute(text('INSERT INTO media VALUES (:id, :id, NULL, :filename)'),
                             dict(id=id, filename=f'{operator}.mp4'))
        for patcher in (
            patch.object(self.main, 'engine', self.database),
            patch.object(self.main, 'get_user', side_effect=lambda username: self.accounts.get(username)),
            patch.object(self.main, 'start_conversion_worker'),
            patch.dict(self.main.app.config, TESTING=True, UPLOAD_FOLDER=str(self.folder)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        self.login('admin')
        self.prefix = self.main.APP_ROOT

    def login(self, username):
        with self.client.session_transaction() as session:
            session.clear()
            session['username'] = username

    def html(self, path, **query):
        response = self.client.get(self.prefix + path, query_string=query)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def export(self, **query):
        response = self.client.get(self.prefix + '/elenco_fototrappole/export.csv', query_string=query)
        self.assertEqual(response.status_code, 200)
        return list(csv.reader(io.StringIO(response.data.decode('utf-8-sig')), delimiter=';'))

    def test_admin_default_lists_all_owners_including_inactive_users(self):
        (self.folder / 'alice.mp4.preview-640.mp4').write_bytes(b'preview')
        cameras = self.html('/elenco_fototrappole')
        sightings = self.html('/sighting_list')
        for html in (cameras, sightings):
            self.assertIn('Tutti gli utenti', html)
            self.assertIn('name="operator"', html)
            self.assertIn('Alice (alice)', html)
            self.assertIn('Bob &lt;B&gt; (bob)', html)
            self.assertIn('Former user (inactive)', html)
            self.assertIn('disattivato', html)
        self.assertEqual(sightings.count('<article'), 4)
        self.assertIn('alice.mp4.preview-640.mp4', sightings)
        self.main.start_conversion_worker.assert_not_called()

    def test_owner_filter_applies_to_both_lists_and_csv(self):
        for path in ('/elenco_fototrappole', '/sighting_list'):
            html = self.html(path, operator='alice')
            self.assertIn('<option value="alice" selected>', html)
            if path.endswith('sighting_list'):
                self.assertEqual(html.count('<article'), 1)
                self.assertNotIn('Bob &lt;B&gt; (bob)</dd>', html)
            else:
                links = Links(html).links
                self.assertEqual([label for _, label in links].count('FT01'), 1)
                export_link = next(url for url, label in links if label == 'Esporta CSV')
                self.assertEqual(parse_qs(urlsplit(export_link).query), {'operator': ['alice']})
        rows = self.export(operator='alice')
        self.assertEqual(rows[0][-1], 'Utente')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][-1], 'Alice (alice)')
        self.assertEqual(len(self.export()), 5)

    def test_duplicate_camera_codes_preserve_owner_in_links_and_coordinates(self):
        links = [url for url, label in Links(self.html('/elenco_fototrappole')).links if label == 'FT01']
        self.assertEqual(len(links), 2)
        for link in links:
            query = parse_qs(urlsplit(link).query)
            owner = query['operator'][0]
            self.assertEqual(query['camtrap_id'], ['FT01'])
            response = self.client.get(link)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertEqual(html.count('<article'), 1)
            self.assertIn(f'data-latitude="{45 if owner == "alice" else 46}.0"', html)
            self.assertIn(f'operator={owner}', html)

    def test_administrator_inspects_other_camera_read_only_and_cannot_post(self):
        path = '/fototrappola/FT01'
        html = self.html(path, operator='alice')
        self.assertIn('Sola lettura', html)
        self.assertIn('<fieldset disabled>', html)
        self.assertIn('const readOnly = true;', html)
        self.assertNotIn('type="submit"', html)
        response = self.client.post(self.prefix + path + '?operator=alice', data={'tipo': 'HACK'})
        self.assertEqual(response.status_code, 403)
        with self.database.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT tipo FROM fototrappole WHERE operator='alice'")).scalar(), 'alice')

    def test_administrator_can_still_edit_own_camera(self):
        self.html('/fototrappola/ADM01', operator='admin')
        with self.client.session_transaction() as session:
            token = session['fototrappola_csrf_token']
        response = self.client.post(self.prefix + '/fototrappola/ADM01?operator=admin', data={
            'csrf_token': token, 'tipo': 'Updated', 'data_inizio': '2026-11-01',
            'latitudine': '47', 'longitudine': '7', 'operator': 'alice',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response.location).query), {'operator': ['admin']})
        with self.database.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT tipo FROM fototrappole WHERE operator='admin'")).scalar(), 'Updated')
            self.assertEqual(conn.execute(text("SELECT tipo FROM fototrappole WHERE operator='alice'")).scalar(), 'alice')

    def test_sighting_detail_and_back_link_retain_admin_filters(self):
        html = self.html('/sighting_list', operator='bob', camtrap_id='FT01')
        link = next(url for url, label in Links(html).links if 'Apri' in label)
        self.assertEqual(parse_qs(urlsplit(link).query), {'operator': ['bob'], 'camtrap_id': ['FT01']})
        response = self.client.get(link)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Bob &lt;B&gt;', html)
        self.assertIn('Immagine non disponibile', html)
        back = next(url for url, label in Links(html).links if label == "Torna all'elenco avvistamenti")
        self.assertEqual(parse_qs(urlsplit(back).query), {'operator': ['bob'], 'camtrap_id': ['FT01']})

    def test_non_admin_cannot_override_scope_or_access_foreign_detail(self):
        self.login('alice')
        with self.client.session_transaction() as session:
            session['is_admin'] = True
        for path in ('/elenco_fototrappole', '/sighting_list'):
            html = self.html(path, operator='bob', is_admin='1')
            self.assertNotIn('name="operator"', html)
            self.assertNotIn('Tutti gli utenti', html)
        rows = self.export(operator='bob')
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 8)
        self.assertEqual(rows[1][1], 'alice')
        self.assertEqual(self.client.get(self.prefix + '/view/2').status_code, 404)
        self.assertEqual(self.client.get(self.prefix + '/view/1').status_code, 200)
        self.assertEqual(self.client.get(self.prefix + '/fototrappola/OLD01?operator=inactive').status_code, 404)
        self.assertEqual(self.client.get(self.prefix + '/sighting_list?camtrap_id=OLD01&operator=inactive').status_code, 404)

    def test_admin_role_revocation_takes_effect_on_next_request(self):
        self.html('/sighting_list')
        self.accounts['admin']['is_admin'] = False
        html = self.html('/sighting_list', operator='alice')
        self.assertEqual(html.count('<article'), 1)
        self.assertNotIn('name="operator"', html)
        self.assertEqual(self.client.get(self.prefix + '/view/1').status_code, 404)

    def test_unknown_owner_filters_and_missing_records_return_404(self):
        for path in ('/sighting_list', '/elenco_fototrappole', '/elenco_fototrappole/export.csv'):
            self.assertEqual(self.client.get(self.prefix + path, query_string={'operator': "unknown' OR 1=1 --"}).status_code, 404)
        self.assertEqual(self.client.get(self.prefix + '/view/999').status_code, 404)
        self.assertEqual(self.client.get(self.prefix + '/fototrappola/MISSING?operator=alice').status_code, 404)

    def test_csv_owner_labels_are_escaped_as_spreadsheet_text(self):
        with self.database.begin() as conn:
            conn.execute(text("UPDATE public.users SET fullname='=formula' WHERE username='bob'"))
        self.assertTrue(self.export(operator='bob')[1][-1].startswith("'=formula"))

    def test_lists_and_detail_require_authentication(self):
        self.client = self.main.app.test_client()
        for path in ('/sighting_list', '/elenco_fototrappole', '/elenco_fototrappole/export.csv', '/view/2', '/fototrappola/FT01?operator=alice'):
            self.assertEqual(self.client.get(self.prefix + path).status_code, 302)
