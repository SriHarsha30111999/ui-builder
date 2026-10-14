from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


def create_db_engine(settings: Settings, database: str | None = None) -> Engine:
    """Create a pooled engine. No connection is opened until first use, so the API can start
    (and report 'database unavailable') even when MySQL is down."""
    return create_engine(
        settings.database_url(database),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,  # stay below MySQL wait_timeout
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            # Store and read all timestamps as UTC; STRICT mode makes bad data an error, not a silent truncation.
            "init_command": "SET time_zone = '+00:00', sql_mode = CONCAT(@@sql_mode, ',STRICT_ALL_TABLES')",
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session, rolling back on error and always releasing the connection."""
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
