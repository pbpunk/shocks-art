from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models import new_id, now_utc


class Media(Base):
    """A source media item independent of the existing livestream-specific models."""

    __tablename__ = "media"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_media_source"),
        UniqueConstraint("checksum_sha256", name="uq_media_checksum"),
    )

    media_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("media"))
    source_type: Mapped[str] = mapped_column(String, default="local", index=True)
    source_id: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String, default="")
    filename: Mapped[str] = mapped_column(String, default="")
    mime_type: Mapped[str] = mapped_column(String, default="application/octet-stream")
    media_kind: Mapped[str] = mapped_column(String, default="unknown", index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    source_modified_ns: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_path: Mapped[str] = mapped_column(Text, default="")
    processing_status: Mapped[str] = mapped_column(String, default="discovered", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
