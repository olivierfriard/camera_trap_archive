"""Persistent MP4 conversions and Google Drive archive processing."""

import hashlib
import logging
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import text


LOGGER = logging.getLogger(__name__)
WORKER_LOCK = 218745092
MAX_ATTEMPTS = 3


def file_md5(path):
    """Compute a file hash without loading the complete video into memory."""
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "md5").hexdigest()


def enqueue_conversion(conn, filename, operator, source_md5):
    """Queue a separate archive MP4, including when the source is already MP4."""
    if Path(filename).name != filename:
        raise ValueError("A source filename without directories is required")
    conn.execute(text("""
        INSERT INTO video_conversions
            (source_filename, operator, source_md5, output_filename)
        VALUES (:source, :operator, :md5, :output)
        ON CONFLICT (source_filename) DO NOTHING
    """), {
        "source": filename, "operator": operator, "md5": source_md5,
        "output": filename + ".compressed.mp4",
    })


def convert_video(source, output, timeout=3600):
    """Use easy_video_compressor's H.264 settings without reducing resolution."""
    temporary = None
    try:
        with NamedTemporaryFile(dir=output.parent, suffix=".mp4", delete=False) as file:
            temporary = Path(file.name)
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-map_metadata", "0", "-map_metadata:s", "0:s", "-map_chapters", "0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-threads", "2", "-y", str(temporary),
        ], check=True, capture_output=True, timeout=timeout)
        if temporary.stat().st_size == 0:
            raise OSError("FFmpeg produced an empty MP4")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def process_one(engine, upload_folder, timeout=3600):
    """Claim one due job, release its transaction, then publish its result atomically."""
    with engine.begin() as conn:
        job = conn.execute(text("""
            SELECT source_filename, source_md5, output_filename, attempts
            FROM video_conversions
            WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP
            ORDER BY created_at, source_filename
            LIMIT 1 FOR UPDATE SKIP LOCKED
        """)).mappings().fetchone()
        if job is None:
            return False
        job = dict(job)
        conn.execute(text("""
            UPDATE video_conversions
            SET status = 'processing', attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP, finished_at = NULL, last_error = NULL
            WHERE source_filename = :source
        """), {"source": job["source_filename"]})

    source = Path(upload_folder) / job["source_filename"]
    output = Path(upload_folder) / job["output_filename"]
    try:
        if source.name != job["source_filename"] or output.name != job["output_filename"]:
            raise ValueError("Conversion paths must remain inside the upload directory")
        if file_md5(source) != job["source_md5"]:
            raise ValueError("Source video no longer matches its upload hash")
        # A previously published file can survive a crash before the database commit.
        if not output.is_file() or output.stat().st_mtime < source.stat().st_mtime or output.stat().st_size == 0:
            convert_video(source, output, timeout=timeout)
        output_md5 = file_md5(output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        details = error.stderr.decode("utf-8", errors="replace") if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        LOGGER.warning("Conversion failed for %s: %s", source.name, details[-2000:])
        attempts = job["attempts"] + 1
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE video_conversions
                SET status = :status, last_error = :error,
                    next_attempt_at = CURRENT_TIMESTAMP + (:delay * INTERVAL '1 second'),
                    finished_at = CASE WHEN :status = 'failed' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE source_filename = :source
            """), {
                "source": job["source_filename"], "error": details[-2000:],
                "status": "failed" if attempts >= MAX_ATTEMPTS else "pending",
                "delay": 60 * (5 ** (attempts - 1)),
            })
        return True

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE video_conversions
            SET status = 'completed', output_md5 = :md5,
                finished_at = CURRENT_TIMESTAMP, last_error = NULL
            WHERE source_filename = :source
        """), {"source": job["source_filename"], "md5": output_md5})
        conn.execute(text("""
            UPDATE media SET converted_file_name = :output WHERE new_file_name = :source
        """), {"source": job["source_filename"], "output": job["output_filename"]})
    LOGGER.info("Archive MP4 ready: %s", output.name)
    return True


def run_worker(engine, upload_folder, watch=False, timeout=3600,
               archive_storage=None, rclone_binary="rclone", archive_timeout=3600):
    """Run at most one worker per database, recovering interrupted conversions."""
    with engine.connect() as guard:
        acquired = guard.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}).scalar()
        guard.commit()
        if not acquired:
            return
        try:
            if archive_storage:
                from archive_transfer import enqueue_ready_transfers, process_one_transfer, recover_transfers
                with engine.begin() as conn:
                    recover_transfers(conn)
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE video_conversions
                    SET status = CASE WHEN attempts >= :maximum THEN 'failed' ELSE 'pending' END,
                        next_attempt_at = CURRENT_TIMESTAMP,
                        finished_at = CASE WHEN attempts >= :maximum THEN CURRENT_TIMESTAMP ELSE NULL END,
                        last_error = 'Previous worker stopped before completing the job'
                    WHERE status = 'processing'
                """), {"maximum": MAX_ATTEMPTS})
            while True:
                converted = process_one(engine, upload_folder, timeout)
                transferred = False
                if archive_storage:
                    with engine.begin() as conn:
                        enqueue_ready_transfers(conn, archive_storage)
                    transferred = process_one_transfer(engine, upload_folder, rclone_binary, archive_timeout)
                if not converted and not transferred:
                    if not watch:
                        return
                    time.sleep(5)
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
            guard.commit()
