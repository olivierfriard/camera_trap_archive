"""Recover archived MP4s locally through the persistent worker queue."""

import logging
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import text

from video_conversion import MAX_ATTEMPTS, file_md5


LOGGER = logging.getLogger(__name__)


def enqueue_download(conn, filename):
    """Queue a recovery, preserving active jobs and allowing explicit retries."""
    conn.execute(text("""
        INSERT INTO archive_downloads (source_filename) VALUES (:filename)
        ON CONFLICT (source_filename) DO UPDATE
        SET status = 'pending', attempts = 0, next_attempt_at = CURRENT_TIMESTAMP,
            started_at = NULL, finished_at = NULL, last_error = NULL
        WHERE archive_downloads.status IN ('completed', 'failed')
    """), {"filename": filename})


def download_archive(remote, output, expected_md5, rclone_binary="rclone", timeout=3600):
    """Publish a recovered MP4 only after verifying its complete local contents."""
    output = Path(output)
    if not output.name.endswith('.compressed.mp4'):
        raise ValueError("Only compressed archive MP4s may be recovered")
    download_verified_file(remote, output, expected_md5, rclone_binary, timeout)


def download_verified_file(remote, output, expected_md5, rclone_binary="rclone", timeout=3600):
    """Recover a file through a verified temporary copy and atomic publication."""
    output = Path(output)
    if output.exists():
        if output.is_file() and file_md5(output) == expected_md5:
            return
        raise ValueError("An unexpected local file already exists")
    temporary = None
    try:
        with NamedTemporaryFile(dir=output.parent, prefix='.archive-restore-', suffix='.part', delete=False) as file:
            temporary = Path(file.name)
        subprocess.run([
            rclone_binary, 'copyto', remote, str(temporary),
            '--checksum', '--retries', '3', '--retries-sleep', '10s', '--log-level', 'ERROR',
        ], check=True, capture_output=True, timeout=timeout)
        if file_md5(temporary).lower() != expected_md5.lower():
            raise ValueError("Recovered file does not match its archive MD5")
        # A complete, verified file becomes visible in a single filesystem operation.
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def process_one_download(engine, upload_folder, rclone_binary="rclone", timeout=3600):
    """Claim one queued recovery and release the database during the copy."""
    with engine.begin() as conn:
        job = conn.execute(text("""
            SELECT d.source_filename, d.attempts, t.output_filename, t.output_md5, t.destination
            FROM archive_downloads d
            JOIN archive_transfers t ON t.source_filename = d.source_filename
            WHERE d.status = 'pending' AND d.next_attempt_at <= CURRENT_TIMESTAMP
                AND t.status = 'completed'
            ORDER BY d.created_at, d.source_filename
            LIMIT 1 FOR UPDATE OF d SKIP LOCKED
        """)).mappings().fetchone()
        if job is None:
            return False
        job = dict(job)
        conn.execute(text("""
            UPDATE archive_downloads
            SET status = 'processing', attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP, finished_at = NULL, last_error = NULL
            WHERE source_filename = :filename
        """), {"filename": job['source_filename']})
    try:
        if Path(job['output_filename']).name != job['output_filename']:
            raise ValueError("Archive filename must remain inside the upload directory")
        download_archive(
            job['destination'], Path(upload_folder) / job['output_filename'], job['output_md5'],
            rclone_binary=rclone_binary, timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        details = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            details = error.stderr.decode('utf-8', errors='replace') if isinstance(error.stderr, bytes) else error.stderr
        LOGGER.warning("Archive recovery failed for %s: %s", job['output_filename'], details[-2000:])
        attempts = job['attempts'] + 1
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE archive_downloads
                SET status = :status, last_error = :error,
                    next_attempt_at = CURRENT_TIMESTAMP + (:delay * INTERVAL '1 second'),
                    finished_at = CASE WHEN :status = 'failed' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE source_filename = :filename
            """), {
                'filename': job['source_filename'], 'error': details[-2000:],
                'status': 'failed' if attempts >= MAX_ATTEMPTS else 'pending',
                'delay': 60 * (5 ** (attempts - 1)),
            })
        return True
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE archive_downloads SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP, last_error = NULL
            WHERE source_filename = :filename
        """), {'filename': job['source_filename']})
    LOGGER.info("Archive MP4 recovered and verified: %s", job['output_filename'])
    return True


def recover_downloads(conn):
    """Retry recoveries interrupted by a worker restart."""
    conn.execute(text("""
        UPDATE archive_downloads
        SET status = CASE WHEN attempts >= :maximum THEN 'failed' ELSE 'pending' END,
            next_attempt_at = CURRENT_TIMESTAMP,
            finished_at = CASE WHEN attempts >= :maximum THEN CURRENT_TIMESTAMP ELSE NULL END,
            last_error = 'Previous worker stopped before completing the recovery'
        WHERE status = 'processing'
    """), {'maximum': MAX_ATTEMPTS})
