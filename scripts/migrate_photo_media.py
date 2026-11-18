"""Add photo metadata and persistent archive queues without changing existing media."""
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SCHEMA = """
ALTER TABLE public.media ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'video'
    CHECK (media_type IN ('video', 'photo'));
ALTER TABLE public.media ADD COLUMN IF NOT EXISTS preview_file_name TEXT;
ALTER TABLE public.media ADD COLUMN IF NOT EXISTS capture_metadata JSONB;
CREATE TABLE IF NOT EXISTS public.photo_archive_transfers (
    media_id INTEGER PRIMARY KEY REFERENCES public.media(id),
    source_filename TEXT NOT NULL UNIQUE,
    source_md5 TEXT NOT NULL,
    destination TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS public.photo_archive_downloads (
    media_id INTEGER PRIMARY KEY REFERENCES public.photo_archive_transfers(media_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT
);
"""


def main():
    config = dotenv_values(Path(__file__).resolve().parent.parent / '.env')
    engine = create_engine(make_url(config['db_url']).set(password=None))
    try:
        with engine.begin() as conn:
            conn.execute(text(SCHEMA))
    finally:
        engine.dispose()
    print('Photo metadata and archive queues created.')


if __name__ == '__main__':
    main()
