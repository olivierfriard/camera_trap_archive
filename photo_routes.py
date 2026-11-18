"""Authenticated photo upload, sighting persistence and archive controls."""

import base64
import hashlib
import json
import math
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image, UnidentifiedImageError
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from photo_archive import enqueue_photo, enqueue_photo_download
from photo_media import PHOTO_EXTENSIONS, md5_file, photo_path, prepare_photo, thumbnail_bytes


def metadata_dict(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def serializer(main):
    return URLSafeTimedSerializer(main.app.secret_key, salt='photo-upload-v1')


def uploaded_photo(main):
    """Authenticate the staged upload independently of all editable form values."""
    try:
        data = serializer(main).loads(request.form.get('photo_upload_token', ''), max_age=86400)
        if data['operator'] != session['username']:
            abort(403)
        path = photo_path(main.app.config['UPLOAD_FOLDER'], data['filename'])
        if data['preview_file_name'] != path.name + '.preview-1280.jpg':
            abort(400)
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        abort(400)
    if not path.is_file():
        abort(410)
    if md5_file(path) != data['md5']:
        abort(409)
    return data, path


def photo_form(main, data, token, values=None, status=200):
    values = dict(values or {})
    with main.engine.connect() as conn:
        cameras = conn.execute(text('SELECT * FROM fototrappole WHERE operator=:operator ORDER BY codice'),
                               {'operator': session['username']}).mappings().all()
    camera = next((row for row in cameras if row['codice'] == values.get('camtrap_id')), None)
    selected = values.pop('date', data.get('date', ''))
    clock = values.pop('time_', data.get('time_', ''))
    default_code = ('A' + selected[2:].replace('-', '') if selected else 'A') + session.get('code', '')
    values.setdefault('code', default_code)
    values.setdefault('wolf_number', '')
    values.setdefault('scalp', '')
    values.setdefault('notes', '')
    return render_template('upload_camtrap_sighting.html', is_photo=True,
                           photo_upload_token=token, capture_metadata=data['capture_metadata'],
                           photo_preview_url=main.media_preview_url(data['filename'], 'photo', data['preview_file_name']),
                           new_file_name=data['filename'], original_file_name=data['original_file_name'], file_content_md5=data['md5'],
                           video_url=None, date=selected, time_=clock, operator=session.get('fullname', session['username']),
                           fototrappole=cameras, fototrappola=camera, **values), status


def save_photo_sighting(main):
    data, path = uploaded_photo(main)
    values = {key: request.form.get(key, '').strip() for key in (
        'code', 'camtrap_id', 'date', 'time_', 'latitude', 'longitude', 'wolf_number', 'scalp',
    )}
    values['notes'] = request.form.get('note', '')
    errors = []
    if not values['code']:
        errors.append("Il codice dell'avvistamento è obbligatorio.")
    try:
        wolves = int(values['wolf_number'])
        if not 0 <= wolves <= 120:
            raise ValueError
    except ValueError:
        errors.append('Il numero di lupi deve essere un intero tra 0 e 120.')
    if values['scalp'] not in ('C1', 'C3'):
        errors.append('Seleziona un valore SCALP valido.')
    stamp = None
    if values['date'] or values['time_']:
        try:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    stamp = datetime.strptime(values['date'] + ' ' + values['time_'], fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError
            stamp = stamp.replace(tzinfo=ZoneInfo(main.app.config['SIGHTING_TIMEZONE']))
            if stamp.astimezone(timezone.utc).astimezone(stamp.tzinfo).replace(tzinfo=None) != stamp.replace(tzinfo=None):
                raise ValueError
        except ValueError:
            errors.append('Data o ora non valide. Verifica data, ora e fuso orario.')
    lat = lon = None
    coordinates_submitted = 'latitude' in request.form or 'longitude' in request.form
    with main.engine.connect() as conn:
        camera = conn.execute(text('SELECT * FROM fototrappole WHERE codice=:code AND operator=:operator'),
                              dict(code=values['camtrap_id'], operator=session['username'])).mappings().fetchone() if values['camtrap_id'] else None
        if values['camtrap_id'] and camera is None:
            errors.append('La fototrappola selezionata non è disponibile per il tuo utente.')
        if not coordinates_submitted and camera is not None:
            values['latitude'], values['longitude'] = camera['latitudine'], camera['longitudine']
        if any(value is not None and str(value).strip() for value in (values['latitude'], values['longitude'])):
            try:
                lat, lon = float(values['latitude']), float(values['longitude'])
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                errors.append('Coordinate non valide. Compila entrambe le coordinate oppure lascia entrambe vuote.')
        if conn.execute(text('SELECT COUNT(*) FROM sighting WHERE code=:code'), {'code': values['code']}).scalar():
            errors.append("Il codice dell'avvistamento è già presente nel database.")
        if conn.execute(text('SELECT COUNT(*) FROM media WHERE file_content_md5=:md5 OR new_file_name=:filename'),
                        dict(md5=data['md5'], filename=data['filename'])).scalar():
            errors.append('Questa foto è già stata registrata.')
    if errors:
        for error in errors:
            flash(error, 'danger')
        return photo_form(main, data, request.form['photo_upload_token'], values, 400)
    storage = main.config.get('archive_storage')
    if not storage:
        flash("L'archivio non è configurato. Contatta l'amministratore.", 'danger')
        return photo_form(main, data, request.form['photo_upload_token'], values, 503)
    preview = path.with_name(data['preview_file_name'])
    try:
        image = thumbnail_bytes(preview)
        metadata = dict(data['capture_metadata'])
        metadata['initial_selection'] = next((item['source'] for item in metadata.get('candidates', [])
            if stamp is not None and item['date'] == stamp.strftime('%Y-%m-%d') and item['time'] == stamp.strftime('%H:%M:%S')), 'manual')
        with main.engine.begin() as conn:
            sighting_id = conn.execute(text('''INSERT INTO sighting
                (code, operator, institution, timestamp, camtrap_id, wolf_number, scalp, latitude, longitude, notes)
                VALUES (:code, :operator, :institution, :timestamp, :camtrap_id, :wolves, :scalp, :lat, :lon, :notes)
                RETURNING id'''), dict(code=values['code'], operator=session['username'], institution=session['institution'],
                                       timestamp=stamp, camtrap_id=values['camtrap_id'] or None, wolves=wolves,
                                       scalp=values['scalp'], lat=lat, lon=lon, notes=values['notes'] or None)).scalar_one()
            media_id = conn.execute(text('''INSERT INTO media
                (sighting_id, original_file_name, new_file_name, file_content_md5, image, media_type, preview_file_name, capture_metadata)
                VALUES (:sighting_id, :original, :filename, :md5, :image, 'photo', :preview, :metadata) RETURNING id''').bindparams(bindparam('metadata', type_=JSON)),
                dict(sighting_id=sighting_id, original=data['original_file_name'], filename=path.name, md5=data['md5'],
                     image=image, preview=preview.name, metadata=metadata)).scalar_one()
            enqueue_photo(conn, media_id, path.name, data['md5'], storage)
    except (OSError, ValueError, IntegrityError):
        main.app.logger.exception('Cannot persist uploaded photo')
        flash('Impossibile salvare la foto. Verifica i dati e riprova.', 'danger')
        return photo_form(main, data, request.form['photo_upload_token'], values, 400)
    except Exception:
        main.app.logger.exception('Photo sighting transaction failed')
        flash('Errore durante il salvataggio. Le modifiche non sono state salvate; riprova.', 'danger')
        return photo_form(main, data, request.form['photo_upload_token'], values, 500)
    main.start_conversion_worker()
    flash('Avvistamento da foto salvato. Archiviazione in corso.', 'success')
    return redirect(url_for('view', sighting_id=sighting_id))


def register_photo_routes(app, main):
    @app.route(main.APP_ROOT + '/upload_camtrap_photo_form', methods=['GET'], endpoint='upload_camtrap_photo_form')
    @main.login_required
    def photo_upload_form():
        session.setdefault('photo_upload_csrf_token', secrets.token_urlsafe(32))
        return render_template('upload_camtrap_photo.html')

    @app.route(main.APP_ROOT + '/upload_camtrap_photo', methods=['POST'], endpoint='upload_camtrap_photo')
    @main.login_required
    def photo_upload():
        if not secrets.compare_digest(session.get('photo_upload_csrf_token', '').encode(), request.form.get('csrf_token', '').encode()) or not session.get('photo_upload_csrf_token'):
            abort(400)
        upload = request.files.get('photo')
        name = secure_filename(upload.filename or '') if upload else ''
        if not upload or Path(name).suffix.lower() not in PHOTO_EXTENSIONS:
            flash('Seleziona una foto JPEG o PNG.', 'danger')
            return redirect(url_for('upload_camtrap_photo_form'))
        digest = hashlib.file_digest(upload.stream, 'md5').hexdigest()
        upload.stream.seek(0)
        with main.engine.connect() as conn:
            duplicate = conn.execute(text('SELECT COUNT(*) FROM media WHERE file_content_md5=:md5'), {'md5': digest}).scalar()
        if duplicate:
            flash('Questa foto è già presente nel database.', 'danger')
            return redirect(url_for('upload_camtrap_photo_form'))
        filename = f"{int(time.time())}_{secure_filename(session['username'])}_{secrets.token_hex(6)}{Path(name).suffix.lower()}"
        path = photo_path(app.config['UPLOAD_FOLDER'], filename)
        upload.save(path)
        try:
            prepared = prepare_photo(path, app.config['SIGHTING_TIMEZONE'], app.config['PHOTO_OCR_TIMEOUT_SECONDS'])
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            main.app.logger.warning('Photo preparation failed for %s', path.name, exc_info=True)
            path.unlink(missing_ok=True)
            path.with_name(path.name + '.preview-1280.jpg').unlink(missing_ok=True)
            flash('Foto non valida o formato non supportato. Usa JPEG o PNG.', 'danger')
            return redirect(url_for('upload_camtrap_photo_form'))
        data = dict(filename=filename, original_file_name=name, md5=digest, operator=session['username'], **prepared)
        token = serializer(main).dumps(data)
        for message in prepared['capture_metadata'].get('warnings', []):
            flash(message, 'warning')
        return photo_form(main, data, token)

    def archive_media(conn, media_id, lock=False):
        clause = '' if main.g.current_user['is_admin'] else ' AND s.operator=:operator'
        params = dict(id=media_id, operator=session['username'])
        query = '''SELECT m.*, t.status AS transfer_status, t.destination, t.source_md5
            FROM media m JOIN sighting s ON s.id=m.sighting_id
            JOIN photo_archive_transfers t ON t.media_id=m.id
            WHERE m.id=:id AND m.media_type='photo'
                AND m.new_file_name=t.source_filename AND m.file_content_md5=t.source_md5 ''' + clause
        if lock:
            query += ' FOR UPDATE OF m'
        row = conn.execute(text(query), params).mappings().fetchone()
        if row is None:
            abort(404)
        return row

    @app.route(main.APP_ROOT + '/photo_archive_status/<int:media_id>', endpoint='photo_archive_status')
    @main.login_required
    def photo_status(media_id):
        with main.engine.connect() as conn:
            row = archive_media(conn, media_id)
            recovery = conn.execute(text('SELECT status FROM photo_archive_downloads WHERE media_id=:id'), {'id': media_id}).mappings().fetchone()
        session.setdefault('archive_restore_csrf_token', secrets.token_urlsafe(32))
        local = photo_path(app.config['UPLOAD_FOLDER'], row['new_file_name']).is_file()
        return jsonify(status='completed', saved=True, transfer_status=row['transfer_status'],
                       restore_status=recovery['status'] if recovery else None,
                       download_url=url_for('uploaded_file', filename=row['new_file_name']) if local else None,
                       restore_url=url_for('restore_archive_photo', media_id=media_id) if not local and row['transfer_status']=='completed' else None,
                       restore_csrf_token=session['archive_restore_csrf_token'])

    @app.route(main.APP_ROOT + '/restore_archive_photo/<int:media_id>', methods=['POST'], endpoint='restore_archive_photo')
    @main.login_required
    def photo_restore(media_id):
        token = session.get('archive_restore_csrf_token', '')
        if not token or not secrets.compare_digest(token.encode(), request.headers.get('X-CSRF-Token', '').encode()):
            abort(400)
        with main.engine.begin() as conn:
            row = archive_media(conn, media_id, lock=True)
            if row['transfer_status'] != 'completed':
                abort(409)
            if photo_path(app.config['UPLOAD_FOLDER'], row['new_file_name']).is_file():
                return jsonify(status='completed')
            enqueue_photo_download(conn, media_id)
        main.start_conversion_worker()
        return jsonify(status='pending'), 202
