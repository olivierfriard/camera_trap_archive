"""Add nullable pup counts and anomaly descriptions without changing old sightings."""
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SCHEMA = """
ALTER TABLE public.sighting ADD COLUMN IF NOT EXISTS pup_number INTEGER
    CHECK (pup_number >= 0);
ALTER TABLE public.sighting ADD COLUMN IF NOT EXISTS anomaly TEXT;
"""


def main():
    config = dotenv_values(Path(__file__).resolve().parent.parent / '.env')
    engine = create_engine(make_url(config['db_url']).set(password=None))
    try:
        with engine.begin() as conn:
            conn.execute(text(SCHEMA))
    finally:
        engine.dispose()
    print('Optional sighting fields created.')


if __name__ == '__main__':
    main()
