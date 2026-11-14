"""Create persistent Google Drive transfer tracking without changing media data."""

from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    root = Path(__file__).resolve().parent.parent
    config = dotenv_values(root / ".env")
    engine = create_engine(make_url(config["db_url"]).set(password=None))
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE public.archive_transfers (
                source_filename TEXT PRIMARY KEY REFERENCES public.video_conversions(source_filename),
                output_filename TEXT NOT NULL,
                output_md5 TEXT NOT NULL,
                destination TEXT NOT NULL UNIQUE,
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
    print("Archive transfer queue created.")


if __name__ == "__main__":
    main()
