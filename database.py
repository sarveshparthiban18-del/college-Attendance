
import os
import sqlite3
from flask import g


# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "attendancepro.db"
    )
)


# --------------------------------------------------
# CROSS-DATABASE ERROR HANDLING
# --------------------------------------------------

try:
    import psycopg

    INTEGRITY_ERRORS = (
        sqlite3.IntegrityError,
        psycopg.IntegrityError
    )

    DATABASE_ERRORS = (
        sqlite3.Error,
        psycopg.Error
    )

except ImportError:
    INTEGRITY_ERRORS = (
        sqlite3.IntegrityError,
    )

    DATABASE_ERRORS = (
        sqlite3.Error,
    )


# --------------------------------------------------
# HYBRID ROW
# Supports row["column"] and row[0]
# --------------------------------------------------

class HybridRow(dict):

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]

        return super().__getitem__(key)


def hybrid_row(cursor):

    def make_row(values):
        columns = [
            column.name
            for column in cursor.description
        ]

        return HybridRow(columns, values)

    return make_row


# --------------------------------------------------
# DATABASE CONNECTION
# SQLite locally / PostgreSQL in cloud
# --------------------------------------------------

def get_db():

    if "db" not in g:

        if DATABASE_URL:
            import psycopg

            g.db = psycopg.connect(
                DATABASE_URL,
                row_factory=hybrid_row,
                connect_timeout=10
            )

        else:
            connection = sqlite3.connect(
                DATABASE_PATH,
                timeout=10
            )

            connection.row_factory = sqlite3.Row

            connection.execute(
                "PRAGMA busy_timeout=5000"
            )

            connection.execute(
                "PRAGMA foreign_keys=ON"
            )

            g.db = connection

    return g.db


# --------------------------------------------------
# CLOSE DATABASE CONNECTION
# --------------------------------------------------

def close_db(error=None):

    connection = g.pop("db", None)

    if connection is not None:
        connection.close()
