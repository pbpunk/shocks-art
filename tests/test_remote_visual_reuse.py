from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.indexing.media_retrieval import MaterializedMedia
from app.indexing.service import VisualExtractionConfig, index_visual_media
from app.library_models import Media


class FakeFrameBackend:
    name = "remote-reuse-frame"
    version = "1"

    def extract_frame(self, source_path, timestamp_ms, output_path, *, still_image):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"{source_path.name}:{timestamp_ms}".encode())


class CountingRetriever:
    def __init__(self, source_path: Path):
        self.source_path = source_path
        self.calls = 0

    @contextmanager
    def materialize(self, media):
        self.calls += 1
        yield MaterializedMedia(
            path=self.source_path,
            temporary=True,
            source_type=media.source_type,
            size_bytes=self.source_path.stat().st_size,
        )


def test_completed_remote_visual_index_does_not_materialize_again(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reuse.db'}")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        media = Media(
            source_type="youtube",
            source_id="video123",
            source_url="https://www.youtube.com/watch?v=video123",
            source_path="",
            title="Remote stream",
            filename="",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=0,
            source_modified_ns=0,
            checksum_sha256="d" * 64,
            duration_seconds=125,
        )
        db.add(media)
        db.commit()
        db.refresh(media)

        source = tmp_path / "temporary-source.mp4"
        source.write_bytes(b"video")
        retriever = CountingRetriever(source)
        backend = FakeFrameBackend()
        config = VisualExtractionConfig(sample_interval_seconds=60)
        index_root = tmp_path / "index"

        first = index_visual_media(
            db,
            media,
            index_root=index_root,
            backend=backend,
            retriever=retriever,
            config=config,
        )
        second = index_visual_media(
            db,
            media,
            index_root=index_root,
            backend=backend,
            retriever=retriever,
            config=config,
        )

        assert first.created == 3
        assert first.reused == 0
        assert second.created == 0
        assert second.reused == 3
        assert retriever.calls == 1
    finally:
        db.close()
