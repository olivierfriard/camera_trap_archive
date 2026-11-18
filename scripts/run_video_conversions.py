"""Run video conversions and photo/video archive jobs; --watch handles new uploads."""

import argparse
import logging
import signal
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from sqlalchemy import create_engine

from video_conversion import run_worker
from archive_transfer import normalize_archive_storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    config = dotenv_values(ROOT / ".env")
    ffmpeg_binary = (config.get("FFMPEG_CMD") or "ffmpeg").strip() or "ffmpeg"
    archive_storage = normalize_archive_storage(config["archive_storage"]) if config.get("archive_storage") else None
    rclone_binary = config.get("rclone_binary") or shutil.which("rclone") or str(Path.home() / ".local/bin/rclone")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = create_engine(config["db_url"], pool_pre_ping=True)
    folder = Path(config["upload_folder"])
    if not folder.is_absolute():
        folder = ROOT / folder

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        run_worker(
            engine, folder, watch=args.watch,
            timeout=int(config.get("conversion_timeout_seconds", "3600")),
            archive_storage=archive_storage, rclone_binary=rclone_binary,
            archive_timeout=int(config.get("archive_timeout_seconds", "3600")),
            ffmpeg_binary=ffmpeg_binary,
        )
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
