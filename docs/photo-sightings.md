# Photo sightings

The photo workflow starts from **Nuovo avvistamento → Avvistamento da foto**.
One JPEG/JPG or PNG is uploaded per sighting. The server checks the actual image
format, computes its MD5 and saves it under an application-generated filename.
The original is never resized, rotated, re-encoded or stripped of its metadata.

## Dates and preview

The application reads EXIF dates with Pillow and runs
`camtrap_banner_decoder.extract_date_time` on the photo banner. The proposed date
priority is `DateTimeOriginal`, then `DateTimeDigitized` (CreateDate), then OCR.
EXIF timezone offsets are used when present; otherwise `SIGHTING_TIMEZONE` applies.
Invalid dates, including nonexistent local daylight-saving times, are ignored.
Filesystem dates and the general EXIF modification date are not capture dates.

The form lists valid candidates and warns about conflicting dates. **Usa questa
data** copies a candidate into the editable date/time fields. The final date is
always the user's submitted value. `media.capture_metadata` retains the original
candidates, the initial proposal and the initial selection; those are upload-time
observations, not a claim about a date subsequently edited by a user or admin.

Set the photo OCR timeout in the production `.env`:

```dotenv
PHOTO_OCR_TIMEOUT_SECONDS=5
```

This setting defaults to five seconds and must be positive and finite. It limits
the Tesseract subprocess, not the entire HTTP upload. `TESSERACT_CMD` continues to
select the executable. OCR failure no longer exits the calling Python process;
unrecognized dates can be entered manually. PNG is supported by the decoder.

The JPEG preview is named `<source>.preview-1280.jpg`, with a maximum side of 1280
pixels and quality 85. EXIF orientation is applied only to the preview. A 640-pixel
JPEG thumbnail is kept in `media.image` as a fallback. Previews work without the
original and are retained by cleanup.

## Saving, archiving and recovery

A signed, owner-bound upload token authenticates the staged file and its hash.
The server verifies that hash again when saving. User-submitted filenames,
checksums and operator fields cannot replace the signed file identity. The token
expires after 24 hours. Data validation errors retain the form and upload token.

The sighting, photo media and archive job are committed in one transaction.
Only confirmed sightings are archived. Abandoned uploads are not transferred or
automatically removed by the verified-archive cleanup, like unsubmitted videos.

The existing detached worker processes photo transfers and recoveries in addition
to videos. `photo_archive_transfers` stores the exact destination at enqueue time,
the original MD5, attempts, state and timestamps. It copies to `archive_storage`
with rclone and verifies the remote MD5 before marking the job completed. No photo
is sent through MP4 conversion. Changes to archive_storage do not relocate jobs
already queued. Interrupted jobs are recovered at startup; failures retry up to
three times with the existing retry delays.

The sighting view shows **Scarica la foto originale** while the original exists
locally. After verified archiving and local removal, **Recupera dall'archivio**
queues a recovery. Recoveries use `photo_archive_downloads` and publish the local
file atomically only after MD5 verification. Active jobs are not duplicated;
failed recoveries allow an explicit retry. Owners and current administrators can
access photo archive status and recovery; other accounts cannot.

Lists, sighting views and edit forms use photo previews rather than video players.
The existing edit permissions and owner-scoped camera choices are preserved.
Editing metadata does not re-upload or modify the original photo.

The existing cron command `scripts/cleanup_local_videos.py` also expires photos.
`ARCHIVE_RETENTION` governs initially archived originals; `RECOVERY_RETENTION`
governs recovered originals, in hours. Defaults must be provided in `.env`, e.g.
48 and 24. Preview and database records are retained. The cleaner requires a
valid preview and a matching local MD5, skips pending/processing recoveries and
coordinates with recovery requests using the media row lock. It never deletes
remote originals. See [cleanup instructions](local-video-cleanup.md).

## Deployment

1. Install the updated dependencies (`uv sync --frozen`); Pillow is now an explicit
   dependency and the lockfile is updated.
2. Before serving the updated application, run this idempotent additive migration
   as the account with PostgreSQL credentials configured in `.pgpass`:

   ```bash
   .venv/bin/python scripts/migrate_photo_media.py
   ```

   It adds `media_type` (default `video`), `preview_file_name`, `capture_metadata`
   and the two photo queues. Existing rows, identifiers and video queues remain.
   No existing column or route is renamed. Queue primary/unique keys cover job,
   source and destination lookup; no extra indexes are added to existing tables.
3. Add PHOTO_OCR_TIMEOUT_SECONDS to production `.env` and confirm archive_storage
   and the existing executable paths are configured.
4. Restart Flask and any existing conversion worker so both load the new code.
   An old worker keeps its advisory lock and will not process the new photo queue.
   Run as the account with the rclone remote and access to the upload folder.
5. Preview cleanup in production with the existing command:

   ```bash
   .venv/bin/python scripts/cleanup_local_videos.py --dry-run
   ```

The cron invocation remains unchanged. Neither production services nor local
workers are started automatically by the migration. Keep development/test and
production databases and upload folders separate.
