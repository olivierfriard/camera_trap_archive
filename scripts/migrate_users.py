"""Migrate users.py once before starting the database-backed application.

Run from the project root with .venv/bin/python scripts/migrate_users.py.
Connection credentials are read from ~/.pgpass. The transaction aborts if the
users table already exists; existing accounts are never overwritten.
"""

import ast
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from werkzeug.security import check_password_hash, generate_password_hash


def main():
    root = Path(__file__).resolve().parent.parent
    module = ast.parse((root / "users.py").read_text())
    users = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "USERS"
            for target in node.targets
        )
    )
    config = dotenv_values(root / ".env")
    engine = create_engine(make_url(config["db_url"]).set(password=None))
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE public.users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                fullname TEXT NOT NULL,
                institution TEXT NOT NULL,
                code TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """))
        for username, account in users.items():
            conn.execute(
                text("""
                    INSERT INTO public.users
                        (username, password_hash, fullname, institution, code)
                    VALUES (:username, :password_hash, :fullname, :institution, :code)
                """),
                {
                    "username": username,
                    "password_hash": generate_password_hash(account["pwd"]),
                    **{
                        field: account[field]
                        for field in ("fullname", "institution", "code")
                    },
                },
            )
        rows = conn.execute(text("SELECT * FROM public.users")).mappings().all()
        if len(rows) != len(users):
            raise ValueError("Migrated account count does not match the source")
        for row in rows:
            account = users[row["username"]]
            if not row["is_active"] or not check_password_hash(
                row["password_hash"], account["pwd"]
            ):
                raise ValueError("Migrated account password or status does not match")
            if any(
                row[field] != account[field]
                for field in ("fullname", "institution", "code")
            ):
                raise ValueError("Migrated account details do not match")
    engine.dispose()
    print(f"Migrated and verified {len(users)} accounts.")


if __name__ == "__main__":
    main()
