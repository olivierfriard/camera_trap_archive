"""Create the persistent archive recovery queue without changing existing media."""

from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS public.archive_downloads (
        source_filename TEXT PRIMARY KEY REFERENCES public.archive_transfers(source_filename),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_error TEXT
    )
"""


def main():
    root = Path(__file__).resolve().parent.parent
    config = dotenv_values(root / '.env')
    engine = create_engine(make_url(config['db_url']).set(password=None))
    try:
        with engine.begin() as conn:
            conn.execute(text(CREATE_TABLE))
    finally:
        engine.dispose()
    print('Archive recovery queue created.')


if __name__ == '__main__':
    main()
