import os
from pathlib import Path

import psycopg


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = BASE_DIR / "postgres_schema.sql"


def init_postgres():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    if not SCHEMA_FILE.exists():
        raise FileNotFoundError(SCHEMA_FILE)

    schema = SCHEMA_FILE.read_text(encoding="utf-8")

    with psycopg.connect(
        database_url,
        connect_timeout=10
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(schema)

    print("PostgreSQL schema initialized successfully.")


if __name__ == "__main__":
    init_postgres()