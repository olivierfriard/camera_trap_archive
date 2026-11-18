"""Persist and verify background transfers of saved archive MP4s with rclone."""

import logging
import re
import subprocess
from pathlib import Path, PurePosixPath

from sqlalchemy import text

from video_conversion import MAX_ATTEMPTS, file_md5


LOGGER = logging.getLogger(__name__)


def normalize_archive_storage(value):
    """Accept OF2/folder or rclone's OF2:folder, never a local directory."""
    value = (value or "").strip()
    if ":" in value:
        remote, folder = value.split(":", 1)
    elif "/" in value:
        remote, folder = value.split("/", 1)
    else:
        raise ValueError("archive_storage must contain a remote and a folder")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
        raise ValueError("Invalid rclone remote name")
    if not folder or folder.startswith("/") or ".." in PurePosixPath(folder).parts or ":" in folder:
        raise ValueError("Invalid archive folder")
    return f"{remote}:{folder.rstrip('/')}"


def enqueue_ready_transfers(conn, storage):
    """Queue only completed MP4s belonging to a saved sighting by the uploader."""
    storage = normalize_archive_storage(storage)
    conn.execute(text("""
        INSERT INTO archive_transfers
            (source_filename, output_filename, output_md5, destination)
        SELECT DISTINCT v.source_filename, v.output_filename, v.output_md5,
                        :storage || '/' || v.output_filename
        FROM video_conversions v
        JOIN media m ON m.new_file_name = v.source_filename
                    AND m.converted_file_name = v.output_filename
        JOIN sighting s ON s.id = m.sighting_id AND s.operator = v.operator
        WHERE v.status = 'completed' AND v.output_md5 IS NOT NULL
        ON CONFLICT (source_filename) DO NOTHING
    """), {"storage": storage})


def copy_archive(source, destination, expected_md5, rclone_binary="rclone", timeout=3600):
    """Copy without deleting files and compare Drive's MD5 with the queued hash."""
    if not source.name.endswith(".compressed.mp4"):
        raise ValueError("Only compressed archive MP4s may be transferred")
    copy_verified_file(source, destination, expected_md5, rclone_binary, timeout)


def copy_verified_file(source, destination, expected_md5, rclone_binary="rclone", timeout=3600):
    """Copy an immutable archive object and verify its local and remote hashes."""
    if file_md5(source) != expected_md5:
        raise ValueError("Local file no longer matches its archive hash")
    # Check existing content explicitly: single-file copyto does not enforce all
    # directory-copy safety flags consistently across rclone backends/versions.
    existing = subprocess.run([
        rclone_binary, "md5sum", destination, "--retries", "1", "--log-level", "ERROR",
    ], check=False, capture_output=True, text=True, timeout=60)
    if existing.returncode == 0 and existing.stdout.strip():
        hashes = [line.split()[0].lower() for line in existing.stdout.splitlines() if line.strip()]
        if hashes == [expected_md5.lower()]:
            return
        raise ValueError("Destination already exists with different content")
    if existing.returncode not in (0, 3, 4):
        raise subprocess.CalledProcessError(
            existing.returncode, existing.args, output=existing.stdout, stderr=existing.stderr,
        )
    subprocess.run([
        rclone_binary, "copyto", str(source), destination,
        "--checksum", "--immutable", "--retries", "3", "--retries-sleep", "10s",
        "--log-level", "ERROR",
    ], check=True, capture_output=True, timeout=timeout)
    result = subprocess.run([
        rclone_binary, "md5sum", destination, "--retries", "3", "--log-level", "ERROR",
    ], check=True, capture_output=True, text=True, timeout=60)
    hashes = [line.split()[0].lower() for line in result.stdout.splitlines() if line.strip()]
    if hashes != [expected_md5.lower()]:
        raise ValueError("Remote MD5 does not match the archived file")


def process_one_transfer(engine, upload_folder, rclone_binary="rclone", timeout=3600):
    """Release the queue transaction before starting the network transfer."""
    with engine.begin() as conn:
        job = conn.execute(text("""
            SELECT source_filename, output_filename, output_md5, destination, attempts
            FROM archive_transfers
            WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP
            ORDER BY created_at, source_filename
            LIMIT 1 FOR UPDATE SKIP LOCKED
        """)).mappings().fetchone()
        if job is None:
            return False
        job = dict(job)
        conn.execute(text("""
            UPDATE archive_transfers
            SET status = 'processing', attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP, finished_at = NULL, last_error = NULL
            WHERE source_filename = :source
        """), {"source": job["source_filename"]})
    try:
        if Path(job["output_filename"]).name != job["output_filename"]:
            raise ValueError("Archive filename must remain inside the upload directory")
        copy_archive(
            Path(upload_folder) / job["output_filename"], job["destination"], job["output_md5"],
            rclone_binary=rclone_binary, timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        details = str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            details = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        LOGGER.warning("Archive transfer failed for %s: %s", job["output_filename"], details[-2000:])
        attempts = job["attempts"] + 1
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE archive_transfers
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
            UPDATE archive_transfers
            SET status = 'completed', finished_at = CURRENT_TIMESTAMP, last_error = NULL
            WHERE source_filename = :source
        """), {"source": job["source_filename"]})
    LOGGER.info("Archive MP4 transferred and verified: %s", job["destination"])
    return True


def recover_transfers(conn):
    """Requeue interrupted copies; stable destinations make retries idempotent."""
    conn.execute(text("""
        UPDATE archive_transfers
        SET status = CASE WHEN attempts >= :maximum THEN 'failed' ELSE 'pending' END,
            next_attempt_at = CURRENT_TIMESTAMP,
            finished_at = CASE WHEN attempts >= :maximum THEN CURRENT_TIMESTAMP ELSE NULL END,
            last_error = 'Previous worker stopped before completing the transfer'
        WHERE status = 'processing'
    """), {"maximum": MAX_ATTEMPTS})
