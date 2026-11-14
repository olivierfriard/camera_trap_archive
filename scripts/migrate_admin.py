"""Add administrator privileges and promote the existing admin account once."""

from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    root = Path(__file__).resolve().parent.parent
    config = dotenv_values(root / ".env")
    engine = create_engine(make_url(config["db_url"]).set(password=None))
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE public.users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        result = conn.execute(text(
            "UPDATE public.users SET is_admin = TRUE WHERE username = 'admin' AND is_active"
        ))
        if result.rowcount != 1:
            raise ValueError("An active admin account is required; migration rolled back")
    engine.dispose()
    print("Administrator role added; existing admin account promoted.")


if __name__ == "__main__":
    main()
