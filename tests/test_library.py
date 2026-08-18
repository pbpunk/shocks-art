from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.library_models import Media
from app.services import library as library_service
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
        assert first.failures == ()

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


def test_local_ingest_reports_file_failure_and_keeps_other_files(tmp_path, monkeypatch):
    inbox = tmp_path / "library_inbox"
    inbox.mkdir()
    (inbox / "bad.jpg").write_bytes(b"bad")
    (inbox / "good.jpg").write_bytes(b"good")

    original_checksum = library_service.file_checksum

    def failing_checksum(path, chunk_size=1024 * 1024):
        if path.name == "bad.jpg":
            raise OSError("simulated hash failure")
        return original_checksum(path, chunk_size)

    monkeypatch.setattr(library_service, "file_checksum", failing_checksum)

    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        result = ingest_local_media(db, inbox)

        assert result.discovered == 2
        assert result.created == 1
        assert result.errors == 1
        assert len(result.failures) == 1
        assert result.failures[0].path == "bad.jpg"
        assert result.failures[0].error_type == "OSError"
        assert "simulated hash failure" in result.failures[0].message
        assert db.scalar(select(Media).where(Media.filename == "good.jpg")) is not None
        assert db.scalar(select(Media).where(Media.filename == "bad.jpg")) is None


def test_media_inventory_is_machine_readable_and_prefix_safe(client, db_session):
    media = Media(
        source_type="local",
        source_id="C:/private/library/example.mp4",
        source_path="C:/private/library/example.mp4",
        source_url="",
        title="Example",
        filename="example.mp4",
        mime_type="video/mp4",
        media_kind="video",
        size_bytes=123456,
        source_modified_ns=1,
        checksum_sha256="a" * 64,
        duration_seconds=12.5,
        width=1920,
        height=1080,
        processing_status="discovered",
        metadata_json={"relative_path": "folder/example.mp4", "ffprobe_available": True},
    )
    db_session.add(media)
    db_session.commit()

    local = client.get("/api/library/media")
    prefixed = client.get("/shocks_art/api/library/media")

    assert local.status_code == 200
    assert prefixed.status_code == 200
    assert local.json()["schemaVersion"] == 1
    assert prefixed.json()["summary"] == local.json()["summary"]

    payload = prefixed.json()
    assert payload["summary"] == {"total": 1, "video": 1, "image": 0, "returned": 1}
    item = payload["items"][0]
    assert item["mediaId"] == media.media_id
    assert item["kind"] == "video"
    assert item["durationSeconds"] == 12.5
    assert item["dimensions"] == {"width": 1920, "height": 1080}
    assert item["sizeBytes"] == 123456
    assert item["processingStatus"] == "discovered"
    assert item["sha256Short"] == "a" * 12
    assert item["relativePath"] == "folder/example.mp4"
    assert "C:/private/library" not in prefixed.text


def test_sampling_plan_api_is_non_mutating_prefix_safe_and_bounded(client, db_session):
    short = Media(
        source_type="local",
        source_id="C:/private/library/short.mp4",
        source_path="C:/private/library/short.mp4",
        title="Short",
        filename="short.mp4",
        mime_type="video/mp4",
        media_kind="video",
        size_bytes=1,
        source_modified_ns=1,
        checksum_sha256="b" * 64,
        duration_seconds=32.2,
        width=1920,
        height=1080,
    )
    still = Media(
        source_type="local",
        source_id="C:/private/library/still.jpg",
        source_path="C:/private/library/still.jpg",
        title="Still",
        filename="still.jpg",
        mime_type="image/jpeg",
        media_kind="image",
        size_bytes=1,
        source_modified_ns=1,
        checksum_sha256="c" * 64,
        width=1000,
        height=1000,
    )
    db_session.add_all([short, still])
    db_session.commit()

    local = client.get("/api/library/indexing/sampling-plan")
    prefixed = client.get("/shocks_art/api/library/indexing/sampling-plan")

    assert local.status_code == 200
    assert prefixed.status_code == 200
    payload = prefixed.json()
    assert payload["schemaVersion"] == 1
    assert payload["mutatesState"] is False
    assert payload["configuration"]["samplingPolicy"] == "adaptive-v1"
    assert payload["summary"]["media"] == 2
    assert payload["summary"]["video"] == 1
    assert payload["summary"]["image"] == 1
    assert payload["summary"]["expectedVisualTraces"] == 8
    assert payload["summary"]["maxVideoSamples"] == 240

    short_plan = next(item for item in payload["items"] if item["filename"] == "short.mp4")
    assert short_plan["intervalSeconds"] == 5.0
    assert short_plan["sampleCount"] == 7
    assert short_plan["timestampsMs"] == [0, 5000, 10000, 15000, 20000, 25000, 30000]

    references = {item["label"]: item for item in payload["referenceDurations"]}
    assert references["3 hours"]["intervalSeconds"] == 60.0
    assert references["3 hours"]["sampleCount"] == 180
    assert references["8 hours"]["intervalSeconds"] == 120.0
    assert references["8 hours"]["sampleCount"] == 240
    assert "C:/private/library" not in prefixed.text
