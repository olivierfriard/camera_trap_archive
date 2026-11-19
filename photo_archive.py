"""Persistent WebP conversion, verified photo transfers, recovery and local expiry."""

import logging
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from archive_download import download_verified_file
from archive_transfer import copy_verified_file, normalize_archive_storage
from photo_media import photo_path, md5_file
from photo_conversion import convert_photo
from video_conversion import MAX_ATTEMPTS

LOGGER = logging.getLogger(__name__)


def enqueue_photo(conn, media_id, filename, md5, storage):
    storage = normalize_archive_storage(storage)
    conn.execute(text('''
        INSERT INTO photo_archive_transfers (media_id, source_filename, source_md5, destination, archive_filename)
        VALUES (:id, :filename, :md5, :destination, :archive_filename)
    '''), dict(id=media_id, filename=filename, md5=md5, archive_filename=filename + '.webp',
               destination=storage + '/' + filename + '.webp'))


def enqueue_photo_download(conn, media_id):
    conn.execute(text('''
        INSERT INTO photo_archive_downloads (media_id) VALUES (:id)
        ON CONFLICT (media_id) DO UPDATE SET status='pending', attempts=0,
            started_at=NULL, finished_at=NULL, last_error=NULL, next_attempt_at=CURRENT_TIMESTAMP
        WHERE photo_archive_downloads.status IN ('completed', 'failed')
    '''), {'id': media_id})


def process_one_photo(engine, upload_folder, rclone_binary='rclone', timeout=3600, download=False):
    """Process one photo job without holding a database transaction during rclone."""
    table = 'photo_archive_downloads' if download else 'photo_archive_transfers'
    eligibility = " AND t.status='completed'" if download else ''
    with engine.begin() as conn:
        row = conn.execute(text(f'''
            SELECT q.media_id, q.attempts, t.source_filename, t.source_md5, t.destination,
                t.archive_filename, t.archive_md5
            FROM {table} q JOIN photo_archive_transfers t ON t.media_id=q.media_id
            WHERE q.status='pending' AND q.next_attempt_at <= CURRENT_TIMESTAMP {eligibility}
            ORDER BY q.created_at, q.media_id LIMIT 1 FOR UPDATE OF q SKIP LOCKED
        ''')).mappings().fetchone()
        if row is None:
            return False
        job = dict(row)
        conn.execute(text(f'''UPDATE {table} SET status='processing', attempts=attempts+1,
            started_at=CURRENT_TIMESTAMP, finished_at=NULL, last_error=NULL WHERE media_id=:id'''), {'id': job['media_id']})
    try:
        local = photo_path(upload_folder, job['archive_filename'] or job['source_filename'], archive=True)
        digest = job['archive_md5'] or job['source_md5']
        if not download and job['archive_filename']:
            if not job['archive_md5']:
                digest = convert_photo(photo_path(upload_folder, job['source_filename']), local, job['source_md5'])
                with engine.begin() as conn:
                    conn.execute(text('UPDATE photo_archive_transfers SET archive_md5=:md5 WHERE media_id=:id'),
                                 dict(id=job['media_id'], md5=digest))
                    conn.execute(text('UPDATE media SET converted_file_name=:filename WHERE id=:id'),
                                 dict(id=job['media_id'], filename=job['archive_filename']))
        if download:
            download_verified_file(job['destination'], local, digest, rclone_binary, timeout)
        else:
            copy_verified_file(local, job['destination'], digest, rclone_binary, timeout)
    except (OSError, ValueError, IndexError, struct.error, subprocess.SubprocessError) as error:
        details = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            details = error.stderr.decode('utf8', errors='replace') if isinstance(error.stderr, bytes) else error.stderr
        LOGGER.warning('Photo archive job failed for %s: %s', job['source_filename'], details[-2000:])
        attempts = job['attempts'] + 1
        with engine.begin() as conn:
            conn.execute(text(f'''UPDATE {table} SET status=:status, last_error=:error,
                next_attempt_at=CURRENT_TIMESTAMP + (:delay * INTERVAL '1 second'),
                finished_at=CASE WHEN :status='failed' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE media_id=:id'''), dict(id=job['media_id'], status='failed' if attempts >= MAX_ATTEMPTS else 'pending',
                                            error=details[-2000:], delay=60 * 5 ** (attempts - 1)))
        return True
    with engine.begin() as conn:
        conn.execute(text(f'''UPDATE {table} SET status='completed', finished_at=CURRENT_TIMESTAMP,
            last_error=NULL WHERE media_id=:id'''), {'id': job['media_id']})
    LOGGER.info('Photo %s and verified: %s', 'recovered' if download else 'archived', job['source_filename'])
    return True


def recover_photo_jobs(conn):
    for table in ('photo_archive_transfers', 'photo_archive_downloads'):
        conn.execute(text(f'''UPDATE {table} SET
            status=CASE WHEN attempts >= :maximum THEN 'failed' ELSE 'pending' END,
            next_attempt_at=CURRENT_TIMESTAMP,
            finished_at=CASE WHEN attempts >= :maximum THEN CURRENT_TIMESTAMP ELSE NULL END,
            last_error='Previous worker stopped before completing the photo job'
            WHERE status='processing' '''), {'maximum': MAX_ATTEMPTS})


def cleanup_local_photos(engine, upload_folder, apply=False, now=None, *, archive_retention, recovery_retention):
    """Retain previews; expire originals and WebP files only after verified archival."""
    from datetime import timedelta
    if any(not isinstance(value, timedelta) or value <= timedelta(0) for value in (archive_retention, recovery_retention)):
        raise ValueError('Retention periods must be positive durations')
    now = now or datetime.now(timezone.utc)
    folder = Path(upload_folder).resolve()
    if not folder.is_dir():
        raise ValueError('Upload directory does not exist')
    summary = dict(eligible_files=0, eligible_bytes=0, deleted_files=0, deleted_bytes=0, skipped_jobs=0, errors=0)
    with engine.connect() as conn:
        ids = conn.execute(text("SELECT media_id FROM photo_archive_transfers WHERE status='completed'")).scalars().all()
    for media_id in ids:
        try:
            with engine.begin() as conn:
                job = conn.execute(text('''
                    SELECT m.new_file_name, m.preview_file_name, t.finished_at, t.source_md5,
                        t.archive_filename, t.archive_md5
                    FROM photo_archive_transfers t JOIN media m ON m.id=t.media_id
                    JOIN sighting s ON s.id=m.sighting_id
                    WHERE t.media_id=:id AND t.status='completed' AND t.finished_at IS NOT NULL
                        AND m.media_type='photo' AND m.new_file_name=t.source_filename
                        AND m.file_content_md5=t.source_md5
                    FOR UPDATE OF m,t SKIP LOCKED
                '''), {'id': media_id}).mappings().fetchone()
                if job is None:
                    summary['skipped_jobs'] += 1
                    continue
                recovery = conn.execute(text('SELECT status, finished_at FROM photo_archive_downloads WHERE media_id=:id FOR UPDATE'), {'id': media_id}).mappings().fetchone()
                if recovery is not None and recovery['status'] in ('pending', 'processing'):
                    summary['skipped_jobs'] += 1
                    continue
                preview_name = job['new_file_name'] + '.preview-1280.jpg'
                preview = folder / preview_name
                if job['preview_file_name'] != preview_name or preview.is_symlink() or not preview.is_file() or not preview.stat().st_size:
                    LOGGER.warning('Photo retained because its preview is missing: %s', job['new_file_name'])
                    continue
                recovered = recovery is not None and recovery['status'] == 'completed'
                # Originals always use archive retention; only recovered archive files use recovery retention.
                files = [(job['new_file_name'], job['source_md5'], job['finished_at'], archive_retention)]
                if job['archive_filename']:
                    if not job['archive_md5']:
                        summary['skipped_jobs'] += 1
                        continue
                    files.append((job['archive_filename'], job['archive_md5'],
                                  recovery['finished_at'] if recovered else job['finished_at'],
                                  recovery_retention if recovered else archive_retention))
                elif recovered:
                    files = [(job['new_file_name'], job['source_md5'], recovery['finished_at'], recovery_retention)]
                for filename, digest, expiry, retention in files:
                    if expiry is None or now-expiry < retention:
                        continue
                    local = photo_path(folder, filename, archive=True)
                    if not local.is_file():
                        continue
                    if md5_file(local) != digest:
                        LOGGER.warning('Photo retained because its local contents changed: %s', local.name)
                        continue
                    size = local.stat().st_size
                    summary['eligible_files'] += 1
                    summary['eligible_bytes'] += size
                    if apply:
                        local.unlink()
                        summary['deleted_files'] += 1
                        summary['deleted_bytes'] += size
                    LOGGER.info('%s photo %s (%s bytes)', 'Deleted' if apply else 'Would delete', local.name, size)
        except (OSError, ValueError):
            summary['errors'] += 1
            LOGGER.exception('Photo cleanup failed: %s', media_id)
    LOGGER.info('Photo cleanup: %s', summary)
    return summary
