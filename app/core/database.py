from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    if raw == ":memory:":
        return None
    return Path(raw)


settings = get_settings()
db_path = _sqlite_path(settings.database_url)
if db_path:
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from app import library_models, models  # noqa: F401
    from app.library_routes import register_library_routes

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
    register_library_routes()


def _ensure_sqlite_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    if "analysis_runs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("analysis_runs")}
    with engine.begin() as connection:
        if "exception_message" not in columns:
            connection.execute(text("ALTER TABLE analysis_runs ADD COLUMN exception_message TEXT NOT NULL DEFAULT ''"))
    candidate_columns = {column["name"] for column in inspector.get_columns("candidate_windows")}
    with engine.begin() as connection:
        if "candidate_rank" not in candidate_columns:
            connection.execute(text("ALTER TABLE candidate_windows ADD COLUMN candidate_rank INTEGER NOT NULL DEFAULT 1"))
        if "transcript_evidence" not in candidate_columns:
            connection.execute(text("ALTER TABLE candidate_windows ADD COLUMN transcript_evidence JSON NOT NULL DEFAULT '[]'"))
        if "visual_evidence" not in candidate_columns:
            connection.execute(text("ALTER TABLE candidate_windows ADD COLUMN visual_evidence JSON NOT NULL DEFAULT '[]'"))
        if "is_favorite" not in candidate_columns:
            connection.execute(text("ALTER TABLE candidate_windows ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0"))
    if "youtube_oauth_credentials" in inspector.get_table_names():
        credential_columns = {column["name"] for column in inspector.get_columns("youtube_oauth_credentials")}
        with engine.begin() as connection:
            if "available_channels" not in credential_columns:
                connection.execute(text("ALTER TABLE youtube_oauth_credentials ADD COLUMN available_channels JSON NOT NULL DEFAULT '[]'"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
