"""Add the persistent conversion queue and the archive MP4 reference once."""

from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    root = Path(__file__).resolve().parent.parent
    config = dotenv_values(root / ".env")
    engine = create_engine(make_url(config["db_url"]).set(password=None))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE public.media ADD COLUMN converted_file_name TEXT"))
        conn.execute(text("""
            CREATE TABLE public.video_conversions (
                source_filename TEXT PRIMARY KEY,
                operator TEXT NOT NULL REFERENCES public.users(username),
                source_md5 TEXT NOT NULL,
                output_filename TEXT NOT NULL UNIQUE,
                output_md5 TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error TEXT
            )
        """))
    engine.dispose()
    print("Video conversion queue and media.converted_file_name created.")


if __name__ == "__main__":
    main()
