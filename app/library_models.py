from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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

    traces: Mapped[list["Trace"]] = relationship(back_populates="media", cascade="all, delete-orphan")
    index_runs: Mapped[list["IndexRun"]] = relationship(back_populates="media", cascade="all, delete-orphan")


class Trace(Base):
    """Timestamped evidence extracted from Media.

    Trace is deliberately generic. Visual frames, transcript/language spans, OCR,
    metadata observations, and future extractors can share the same durable
    evidence model without coupling Media to a particular ML implementation.
    """

    __tablename__ = "traces"
    __table_args__ = (
        CheckConstraint("start_ms >= 0", name="ck_trace_start_nonnegative"),
        CheckConstraint("end_ms >= start_ms", name="ck_trace_end_after_start"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_trace_confidence_range",
        ),
        UniqueConstraint(
            "media_id",
            "trace_type",
            "start_ms",
            "end_ms",
            "extractor",
            "extractor_version",
            "configuration_hash",
            name="uq_trace_extraction_identity",
        ),
        Index("ix_traces_media_type_time", "media_id", "trace_type", "start_ms"),
    )

    trace_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("trace"))
    media_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("media.media_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trace_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    start_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    extractor: Mapped[str] = mapped_column(String, default="")
    extractor_version: Mapped[str] = mapped_column(String, default="")
    configuration_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    media: Mapped[Media] = relationship(back_populates="traces")
    embeddings: Mapped[list["Embedding"]] = relationship(back_populates="trace", cascade="all, delete-orphan")


class Embedding(Base):
    """Regenerable vector representation of a Trace."""

    __tablename__ = "embeddings"
    __table_args__ = (
        CheckConstraint("embedding_dimension > 0", name="ck_embedding_dimension_positive"),
        UniqueConstraint(
            "trace_id",
            "model_id",
            "embedding_dimension",
            name="uq_embedding_trace_model_dimension",
        ),
    )

    embedding_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("embedding"))
    trace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("traces.trace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    dtype: Mapped[str] = mapped_column(String, default="float32")
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    normalized: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    trace: Mapped[Trace] = relationship(back_populates="embeddings")


class IndexRun(Base):
    """One observable/resumable indexing-stage attempt for a Media item."""

    __tablename__ = "index_runs"
    __table_args__ = (
        Index("ix_index_runs_media_stage_config", "media_id", "stage", "configuration_hash"),
    )

    index_run_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("indexrun"))
    media_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("media.media_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(String, nullable=False, index=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String, default="queued", nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    statistics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    media: Mapped[Media] = relationship(back_populates="index_runs")
