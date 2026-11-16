"""
Expire local videos after verified archiving while retaining every preview.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

LOGGER = logging.getLogger(__name__)


def retention_from_config(config):
    """Read the two mandatory retention periods as positive numbers of hours."""
    durations = []
    for key in ("ARCHIVE_RETENTION", "RECOVERY_RETENTION"):
        try:
            duration = timedelta(hours=float(config[key]))
            if duration <= timedelta(0):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(f"{key} must be a positive, finite number of hours") from None
        durations.append(duration)
    return tuple(durations)


ELIGIBLE_JOB = """
    SELECT v.source_filename, v.output_filename, t.finished_at AS archived_at
    FROM video_conversions v
    JOIN archive_transfers t ON t.source_filename = v.source_filename
    WHERE v.status = 'completed' AND t.status = 'completed'
        AND t.finished_at IS NOT NULL
        AND t.output_filename = v.output_filename AND t.output_md5 = v.output_md5
        AND EXISTS (
            SELECT 1 FROM media m JOIN sighting s ON s.id = m.sighting_id
            WHERE m.new_file_name = v.source_filename
                AND m.converted_file_name = v.output_filename AND s.operator = v.operator
        )
"""


def local_video_path(folder, filename):
    """
    Reject paths, symbolic links and preview filenames even with malformed metadata.
    """
    if not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError("Video filename must remain inside the upload directory")
    if filename.endswith(".preview-640.mp4"):
        raise ValueError("Previews cannot be deleted")
    path = folder / filename
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Only regular local video files can be deleted")
    return path


def cleanup_local_videos(
    engine, upload_folder, apply=False, now=None, *, archive_retention, recovery_retention
):
    """
    Dry-run by default; serialize deletion with recovery requests and worker claims.
    """
    for duration in (archive_retention, recovery_retention):
        if not isinstance(duration, timedelta) or duration <= timedelta(0):
            raise ValueError("Retention periods must be positive durations")
    folder = Path(upload_folder).resolve()
    if not folder.is_dir():
        raise ValueError("Upload directory does not exist")
    now = now if now is not None else datetime.now(timezone.utc)
    summary = dict(
        eligible_files=0,
        eligible_bytes=0,
        deleted_files=0,
        deleted_bytes=0,
        skipped_jobs=0,
        errors=0,
    )
    with engine.connect() as conn:
        filenames = conn.execute(text(ELIGIBLE_JOB)).mappings().all()
    for item in filenames:
        filename = item["source_filename"]
        try:
            with engine.begin() as conn:
                # Recovery requests take this same conversion row lock before checking disk.
                job = (
                    conn.execute(
                        text(
                            ELIGIBLE_JOB
                            + """
                    AND v.source_filename = :filename
                    FOR UPDATE OF v, t SKIP LOCKED
                """
                        ),
                        {"filename": filename},
                    )
                    .mappings()
                    .fetchone()
                )
                if job is None:
                    summary["skipped_jobs"] += 1
                    continue
                # The recovery worker locks this row when claiming a pending copy.
                recovery = (
                    conn.execute(
                        text("""
                    SELECT status, finished_at FROM archive_downloads
                    WHERE source_filename = :filename FOR UPDATE
                """),
                        {"filename": filename},
                    )
                    .mappings()
                    .fetchone()
                )
                if recovery is not None and recovery["status"] in (
                    "pending",
                    "processing",
                ):
                    LOGGER.info("Skipped active recovery: %s", filename)
                    summary["skipped_jobs"] += 1
                    continue
                if job["output_filename"] != filename + ".compressed.mp4":
                    raise ValueError(
                        "Compressed filename does not match its uploaded source"
                    )
                source = local_video_path(folder, filename)
                compressed = local_video_path(folder, job["output_filename"])
                source_due = now - job["archived_at"] >= archive_retention
                if recovery is not None and recovery["status"] == "completed":
                    compressed_due = (
                        recovery["finished_at"] is not None
                        and now - recovery["finished_at"] >= recovery_retention
                    )
                else:
                    compressed_due = source_due
                preview = source.with_name(source.name + ".preview-640.mp4")
                preview_ready = (
                    not preview.is_symlink()
                    and preview.is_file()
                    and preview.stat().st_size > 0
                    and (
                        not source.exists()
                        or preview.stat().st_mtime >= source.stat().st_mtime
                    )
                )
                for path, due in ((source, source_due), (compressed, compressed_due)):
                    if not due or not path.is_file():
                        continue
                    if path == source and not preview_ready:
                        LOGGER.warning(
                            "Original retained because a current preview is missing: %s",
                            filename,
                        )
                        continue
                    size = path.stat().st_size
                    summary["eligible_files"] += 1
                    summary["eligible_bytes"] += size
                    if apply:
                        path.unlink()
                        summary["deleted_files"] += 1
                        summary["deleted_bytes"] += size
                    LOGGER.info(
                        "%s %s (%d bytes)",
                        "Deleted" if apply else "Would delete",
                        path.name,
                        size,
                    )
        except (OSError, ValueError):
            summary["errors"] += 1
            LOGGER.exception("Local video cleanup failed: %s", filename)
    LOGGER.info(
        "Local video cleanup (%s): %s", "apply" if apply else "dry-run", summary
    )
    return summary
