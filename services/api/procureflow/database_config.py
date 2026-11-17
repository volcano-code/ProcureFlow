"""Synchronous DB URL contract shared by application and migrations.

Do not echo input URLs in errors: a URL can contain passwords/query secrets.
"""
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def normalize_database_url(value: str) -> str:
    try:
        url = make_url(value)
        if url.drivername in {"postgres", "postgresql", "postgresql+psycopg"}:
            if not url.database:
                raise ValueError("DATABASE_NAME_REQUIRED")
            url = url.set(drivername="postgresql+psycopg")
        elif url.drivername not in {"sqlite", "sqlite+pysqlite"}:
            raise ValueError("UNSUPPORTED_DATABASE_DRIVER")
        return url.render_as_string(hide_password=False)
    except ArgumentError:
        raise ValueError("INVALID_DATABASE_URL") from None
