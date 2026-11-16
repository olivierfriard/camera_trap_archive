# Local video cleanup

`scripts/cleanup_local_videos.py` removes local videos after their compressed MP4
has been archived and verified. It never uploads originals and never deletes
previews or database records.

Define both retention periods in the production `.env`, in hours:

```dotenv
ARCHIVE_RETENTION=48
RECOVERY_RETENTION=24
```

Both values are mandatory positive numbers (fractional hours are supported).
Missing, empty, nonpositive or invalid values stop the command before any
cleanup or database connection. The file is read again on every execution,
including when selected with `--config`; no crontab change is needed after
changing a duration. `.env` is not versioned, so add these settings on each
installation before the next scheduled run.

With the above values, the retention periods are:

- Uploaded originals: at least 48 hours after `archive_transfers.finished_at`.
  A nonempty preview at least as recent as the original must remain available;
  otherwise the original is retained and a warning is logged.
- Initially produced compressed MP4s: at least 48 hours after verified archiving.
- Compressed MP4s recovered from Drive: at least 24 hours after the latest
  completed recovery, using `archive_downloads.finished_at`. The local file's
  modification time is not used because rclone may preserve the archived date.

The script only considers completed conversions and completed verified transfers
associated with saved sightings belonging to the uploader. It skips pending or
processing recoveries. It locks each conversion row, as recovery requests do,
and the corresponding recovery row, as the worker does. This prevents cleanup
from deleting a video while a recovery is being queued or processed. Deploy the
updated Flask code before scheduling cleanup so requests use this shared lock.

Only the exact original and compressed filenames from the database are eligible.
Symbolic links, directories, paths outside the upload folder and preview names
are rejected. Database timestamps and archive destinations are kept for future
recoveries. No Google Drive deletion or transfer is performed by this script.

## Manual verification

Run these commands from the production project, as the same Linux account that
runs the application. The command reads that project's `.env`; relative upload
paths are resolved against the project directory.

```bash
.venv/bin/python scripts/cleanup_local_videos.py --dry-run
```

This only lists eligible files and bytes. To perform the cleanup:

```bash
.venv/bin/python scripts/cleanup_local_videos.py --apply
```

An alternative configuration can be selected with `--config /absolute/path/.env`.
Use the production database and production upload directory together. Do not
schedule the development machine against a shared production queue. The script
logs each planned/deleted file and the byte totals; any filesystem error produces
a nonzero exit status. No deletion occurs unless `--apply` is passed.

## Nightly crontab

An example user crontab is provided in `deploy/camera-trap-cleanup.crontab`.
Replace every `/srv/camera_trap_archive` path with the absolute production
project path. Run the job as the Linux account used by the application, with
access to `.env`, database credentials (including `.pgpass` if used), and the
upload directory. No rclone configuration is needed for cleanup.

Create the log directory from that account, adapting the path:

```bash
mkdir -p /srv/camera_trap_archive/instance
```

Run `crontab -e` as that account and add this single line, keeping existing jobs:

```cron
0 3 * * * "/srv/camera_trap_archive/.venv/bin/python" "/srv/camera_trap_archive/scripts/cleanup_local_videos.py" --config "/srv/camera_trap_archive/.env" --apply >> "/srv/camera_trap_archive/instance/video-cleanup.log" 2>&1
```

This is a user crontab entry: it has no username column. The absolute interpreter,
script, configuration and log paths avoid dependence on cron's working directory
or PATH. The `.env` file is read on each run, so retention changes take effect
automatically. Keep the cron daemon enabled on the production server.

The job runs at 03:00 in the server's timezone. Files are removed on the first
scheduled run after their retention period expires. Cron does not catch up on a
run missed while the server was stopped. Cleanup remains separate from the
conversion worker and does not acquire its permanent worker advisory lock.

Inspect the installed schedule and recent output with:

```bash
crontab -l
tail -n 100 /srv/camera_trap_archive/instance/video-cleanup.log
```

To stop future runs, comment out or remove only this line using `crontab -e`.
If the former systemd timer was already installed on the server, disable it
before adding the cron job to avoid scheduling both:

```bash
sudo systemctl disable --now camera-trap-cleanup.timer
```
