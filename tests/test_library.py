from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.library_models import Media
from app.services.library import ingest_local_media, scan_media_files


def test_scan_media_files_creates_missing_inbox(tmp_path):
    inbox = tmp_path / "library_inbox"

    assert scan_media_files(inbox) == []
    assert inbox.is_dir()


def test_local_ingest_is_idempotent_and_deduplicates_content(tmp_path):
    inbox = tmp_path / "library_inbox"
    inbox.mkdir()
    (inbox / "photo.jpg").write_bytes(b"fake-jpeg-content")
    (inbox / "clip.mp4").write_bytes(b"fake-video-content")

    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        first = ingest_local_media(db, inbox)
        assert first.discovered == 2
        assert first.created == 2
        assert first.errors == 0

        items = list(db.scalars(select(Media)).all())
        assert {item.media_kind for item in items} == {"image", "video"}

        second = ingest_local_media(db, inbox)
        assert second.discovered == 2
        assert second.created == 0
        assert second.skipped == 2

        (inbox / "duplicate.jpg").write_bytes(b"fake-jpeg-content")
        third = ingest_local_media(db, inbox)
        assert third.discovered == 3
        assert third.created == 0
        assert third.skipped == 3
        assert db.scalar(select(Media).where(Media.filename == "duplicate.jpg")) is None
