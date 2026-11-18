"""
Expire local archive videos; use --apply to delete rather than simulate.
"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from sqlalchemy import create_engine

from media_cleanup import cleanup_local_videos, retention_from_config
from photo_archive import cleanup_local_photos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument(
        "--dry-run", action="store_true", help="Simulate cleanup (the default)"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not args.config.is_file():
        parser.error("Configuration file does not exist")
    config = dotenv_values(args.config)
    try:
        archive_retention, recovery_retention = retention_from_config(config)
    except ValueError as error:
        parser.error(str(error))
    folder = Path(config["upload_folder"])
    if not folder.is_absolute():
        folder = ROOT / folder
    engine = create_engine(config["db_url"], pool_pre_ping=True)
    try:
        result = cleanup_local_videos(
            engine, folder, apply=args.apply,
            archive_retention=archive_retention, recovery_retention=recovery_retention,
        )
        photos = cleanup_local_photos(
            engine, folder, apply=args.apply,
            archive_retention=archive_retention, recovery_retention=recovery_retention,
        )
        result['errors'] += photos['errors']
    finally:
        engine.dispose()
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
