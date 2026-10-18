"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from localledger.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        else:
            _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    from localledger import models  # noqa: F401  (registers tables)

    SQLModel.metadata.create_all(get_engine())
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Additive migrations: new nullable/defaulted columns on existing tables (no data is rewritten or dropped)."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = col.type.compile(engine.dialect)
                literal = (
                    "" if default is None else f" DEFAULT {int(default) if isinstance(default, bool) else default!r}"
                )
                null = "" if col.nullable or default is not None else " DEFAULT 0"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{literal}{null}'))


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
