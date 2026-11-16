"""Verify camera-trap editing, ownership checks and validation preservation."""

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from sqlalchemy import create_engine, text


class InputParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and 'name' in attrs:
            self.inputs[attrs['name']] = attrs


class FototrappolaEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory()
        config = dict(dotenv_values(Path(__file__).resolve().parent.parent / '.env'))
        config['log_file'] = str(Path(cls.logs.name) / 'app.log')
        with patch('dotenv.dotenv_values', return_value=config):
            import main
        cls.main = main

    @classmethod
    def tearDownClass(cls):
        cls.logs.cleanup()

    def setUp(self):
        self.database = create_engine('sqlite://')
        self.addCleanup(self.database.dispose)
        with self.database.begin() as conn:
            conn.execute(text('''CREATE TABLE fototrappole (
                codice TEXT, operator TEXT, tipo TEXT, data_inizio DATE, data_fine DATE,
                nome TEXT, cognome TEXT, regione TEXT, provincia TEXT, comune TEXT,
                location TEXT, country TEXT, latitudine REAL, longitudine REAL,
                altitudine REAL, intersezioni TEXT
            )'''))
            conn.execute(text('''INSERT INTO fototrappole VALUES
                ('FT01', 'user', 'Camera', '2026-01-02', NULL, 'Nome', 'Cognome',
                 'Piemonte', 'Torino', 'Torino', 'Bosco', 'Italia', 0, 7.6, 0, 'I1'),
                ('FT01', 'other', 'Other camera', '2026-01-02', NULL, '', '',
                 '', '', '', '', 'Italia', 46, 8, 500, ''),
                ('PRIVATE', 'other', 'Private camera', '2026-01-02', NULL, '', '',
                 '', '', '', '', 'Italia', 46, 8, 500, '')
            '''))
        account = dict(username='user', fullname='User', institution='Test', code='T', is_active=True, is_admin=False)
        for patcher in (
            patch.object(self.main, 'engine', self.database),
            patch.object(self.main, 'get_user', return_value=account),
            patch.dict(self.main.app.config, TESTING=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()
        with self.client.session_transaction() as session:
            session['username'] = 'user'
        self.path = self.main.APP_ROOT + '/fototrappola/FT01'

    def data(self, **changes):
        self.client.get(self.path)
        with self.client.session_transaction() as session:
            token = session['fototrappola_csrf_token']
        fields = dict(csrf_token=token, codice='CHANGED', tipo='Updated', data_inizio='2026-02-03',
                      data_fine='', nome='New name', cognome='Cognome', regione='Piemonte',
                      provincia='Torino', comune='Torino', location='New location', country='Italia',
                      latitudine='45.1', longitudine='7.2', altitudine='345', intersezioni='I2')
        fields.update(changes)
        return fields

    def rows(self):
        with self.database.connect() as conn:
            return {row['operator']: dict(row) for row in conn.execute(
                text("SELECT * FROM fototrappole WHERE codice = 'FT01'")
            ).mappings()}

    def test_list_links_to_prefilled_edit_form(self):
        html = self.client.get(self.main.APP_ROOT + '/elenco_fototrappole').get_data(as_text=True)
        self.assertIn(f'href="{self.path}"', html)
        html = self.client.get(self.path).get_data(as_text=True)
        fields = InputParser(html).inputs
        self.assertEqual(fields['codice']['value'], 'FT01')
        self.assertIn('readonly', fields['codice'])
        self.assertEqual(fields['data_inizio']['value'], '2026-01-02')
        self.assertEqual(fields['data_fine']['value'], '')
        self.assertEqual(fields['latitudine']['value'], '0.0')
        self.assertEqual(fields['altitudine']['value'], '0.0')
        self.assertIn('syncFromGeographic(false);', html)
        self.assertIn('Salva modifiche', html)

    def test_update_is_scoped_and_cannot_rename_code_or_operator(self):
        original_other = self.rows()['other']
        response = self.client.post(self.path, data=self.data(operator='other'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/elenco_fototrappole'))
        rows = self.rows()
        self.assertEqual(rows['other'], original_other)
        self.assertEqual(rows['user']['codice'], 'FT01')
        self.assertEqual(rows['user']['tipo'], 'Updated')
        self.assertEqual(rows['user']['latitudine'], 45.1)
        self.assertEqual(rows['user']['location'], 'New location')
        self.assertEqual(rows['user']['data_inizio'], '2026-02-03')
        self.assertIsNone(rows['user']['data_fine'])

    def test_foreign_or_missing_camera_is_not_accessible(self):
        for code in ['PRIVATE', 'MISSING']:
            path = self.main.APP_ROOT + '/fototrappola/' + code
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path, data=self.data()).status_code, 404)

    def test_invalid_fields_preserve_draft_without_updating(self):
        before = self.rows()
        for changes in [dict(latitudine='91'), dict(longitudine='nan'),
                        dict(altitudine='inf'), dict(data_inizio='bad'), dict(tipo='')]:
            with self.subTest(changes=changes):
                response = self.client.post(self.path, data=self.data(nome='<draft>', **changes))
                self.assertEqual(response.status_code, 400)
                html = response.get_data(as_text=True)
                self.assertIn('&lt;draft&gt;', html)
                self.assertEqual(InputParser(html).inputs['nome']['value'], '<draft>')
                self.assertEqual(self.rows(), before)

    def test_invalid_csrf_is_rejected(self):
        before = self.rows()
        for token in ['', 'wrong', 'é']:
            self.assertEqual(self.client.post(self.path, data=self.data(csrf_token=token)).status_code, 400)
        self.assertEqual(self.rows(), before)

    def test_login_required(self):
        self.client.get(self.main.APP_ROOT + '/logout')
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/login'))

    def test_new_camera_form_remains_available(self):
        html = self.client.get(self.main.APP_ROOT + '/nuova_fototrappola').get_data(as_text=True)
        fields = InputParser(html).inputs
        self.assertNotIn('readonly', fields['codice'])
        self.assertEqual(fields['codice']['value'], '')
        self.assertEqual(fields['country']['value'], 'Italia')
        self.assertIn('syncFromGeographic(true);', html)
        self.assertIn('/save_fototrappola', html)
