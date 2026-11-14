# Video conversions

Run the one-time migration before restarting the Flask application:

```bash
.venv/bin/python scripts/migrate_video_conversions.py
.venv/bin/python scripts/migrate_archive_transfers.py
```

Each new video upload queues an archive conversion, starts a detached worker,
and creates a synchronous preview. Previews have a maximum width of 640 pixels,
preserve the aspect ratio, and are cached as `<source>.preview-640.mp4`. MP4
sources also receive a preview. The 15-second preview timeout is unchanged.

Archive conversions produce `<source>.compressed.mp4` without reducing the
resolution. Settings match `easy_video_compressor`: libx264, medium, CRF 24,
AAC 128 kbit/s, copied metadata and chapters, and MP4 faststart. Two encoding
threads limit competition with synchronous previews. Uploaded originals and
their MD5 values remain unchanged.

The PostgreSQL queue survives application and worker restarts. A session advisory
lock permits one archive worker per database. The worker releases job transactions
before running FFmpeg, atomically publishes completed MP4s, and retries failures
up to three times, with one- and five-minute retry delays. The default conversion
timeout is one hour; set `conversion_timeout_seconds` in `.env` to change it.

The worker starts automatically on a new upload or saved sighting and keeps watching the queue.
Its log is `instance/video-conversion.log`. After a machine restart, the next
upload starts the worker again. To resume immediately, or run under an existing
process supervisor, use:

```bash
.venv/bin/python scripts/run_video_conversions.py --watch
```

Without `--watch`, the command processes currently due jobs and exits. Jobs
interrupted by a previous worker are recovered when the next worker starts.
Do not delete source videos while conversions are pending.

`media.new_file_name` and `media.file_content_md5` still describe the upload.
`media.converted_file_name` is populated when its archive MP4 is ready, whether
the sighting is saved before or after conversion finishes. The row-level queue
lock prevents a race between those two events. Old media are not queued
retroactively. Queue primary and unique keys cover filename lookups; no additional
indexes are needed for the current small queue.

## Google Drive transfers

Set `archive_storage` in `.env`, for example:

```dotenv
archive_storage=OF2/test_camera_trap_archive
```

Both `OF2/folder` and rclone's `OF2:folder` syntax are supported. The same worker
queues transfers only for completed archive MP4s associated with a saved sighting
by their uploader. Unsubmitted uploads, originals and previews are not sent.

`archive_transfers` records the destination, checksum, state, attempts and error.
The destination is captured when the transfer is first queued; changing `.env`
does not redirect existing jobs or re-send completed files. Each MP4 is copied
directly into the configured folder under its unique compressed filename.

The worker runs `rclone copyto --checksum --immutable`, then reads the remote MD5
and compares it with `video_conversions.output_md5`. Only a matching checksum
marks the transfer completed. Retries use the same destination and will skip
identical remote content. Existing different content is not overwritten. All
local files are retained.
Before copying, an existing remote file's MD5 is checked explicitly: matching
content is reused, while different content raises an error without overwriting it.
Transfer state is visible both during entry and in the saved sighting view.

Transfer failures retry three times with one- and five-minute delays. Interrupted
transfers are recovered when the worker restarts. The transfer timeout is one
hour by default; set `archive_timeout_seconds` to change it. Set `rclone_binary`
to specify an executable; otherwise PATH and `~/.local/bin/rclone` are checked.
The worker must run as the Linux account with the configured Drive remote.

Restart an already-running worker after updating its code or `.env`; its advisory
lock prevents a new worker from replacing it automatically. No systemd timer is
required. If `archive_storage` is unset, only conversion runs. Logs remain in
`instance/video-conversion.log`.
