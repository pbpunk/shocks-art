from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Stream(Base):
    __tablename__ = "streams"
    __table_args__ = (UniqueConstraint("platform", "source_video_id", name="uq_stream_source"),)

    stream_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("stream"))
    platform: Mapped[str] = mapped_column(String, default="youtube")
    channel_id: Mapped[str] = mapped_column(String, index=True)
    source_video_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String)
    published_at: Mapped[str] = mapped_column(String, default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail: Mapped[str] = mapped_column(String, default="")
    processing_status: Mapped[str] = mapped_column(String, default="queued", index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    analysis_runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="stream", cascade="all, delete-orphan")
    candidates: Mapped[list["CandidateWindow"]] = relationship(back_populates="stream", cascade="all, delete-orphan")
    transcripts: Mapped[list["StreamTranscript"]] = relationship(back_populates="stream", cascade="all, delete-orphan")
    analysis_artifacts: Mapped[list["StreamAnalysisArtifact"]] = relationship(back_populates="stream", cascade="all, delete-orphan")
    youtube_videos: Mapped[list["YouTubeVideo"]] = relationship(back_populates="stream")


class StreamTranscript(Base):
    __tablename__ = "stream_transcripts"

    stream_transcript_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("transcript"))
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.stream_id"), index=True)
    language: Mapped[str] = mapped_column(String, default="en")
    source: Mapped[str] = mapped_column(String, default="youtube_auto_captions")
    format: Mapped[str] = mapped_column(String, default="plain_text")
    text: Mapped[str] = mapped_column(Text, default="")
    raw_location: Mapped[str] = mapped_column(String, default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    stream: Mapped[Stream] = relationship(back_populates="transcripts")


class StreamAnalysisArtifact(Base):
    __tablename__ = "stream_analysis_artifacts"

    stream_analysis_artifact_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("artifact"))
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.stream_id"), index=True)
    analysis_run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_runs.analysis_run_id"), nullable=True, index=True)
    artifact_type: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String, default="")
    artifact_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    stream: Mapped[Stream] = relationship(back_populates="analysis_artifacts")
    analysis_run: Mapped["AnalysisRun | None"] = relationship(back_populates="stream_artifacts")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    analysis_run_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("run"))
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.stream_id"), index=True)
    model: Mapped[str] = mapped_column(String)
    model_version: Mapped[str] = mapped_column(String, default="")
    prompt_version: Mapped[str] = mapped_column(String, default="1.0")
    schema_version: Mapped[str] = mapped_column(String, default="1.0")
    request_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    request_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_response_location: Mapped[str] = mapped_column(String, default="")
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    exception_message: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    stream: Mapped[Stream] = relationship(back_populates="analysis_runs")
    candidates: Mapped[list["CandidateWindow"]] = relationship(back_populates="analysis_run", cascade="all, delete-orphan")
    stream_artifacts: Mapped[list[StreamAnalysisArtifact]] = relationship(back_populates="analysis_run")


class CandidateWindow(Base):
    __tablename__ = "candidate_windows"

    candidate_window_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("candidate"))
    stream_id: Mapped[str] = mapped_column(ForeignKey("streams.stream_id"), index=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.analysis_run_id"), index=True)
    candidate_rank: Mapped[int] = mapped_column(Integer, default=1)
    start_seconds: Mapped[int] = mapped_column(Integer)
    end_seconds: Mapped[int] = mapped_column(Integer)
    start_timestamp: Mapped[str] = mapped_column(String)
    end_timestamp: Mapped[str] = mapped_column(String)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    concise_summary: Mapped[str] = mapped_column(Text)
    selection_reason: Mapped[str] = mapped_column(Text)
    primary_pillar: Mapped[str] = mapped_column(String, index=True)
    secondary_pillars: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    visual_description: Mapped[str] = mapped_column(Text, default="")
    transcript_evidence: Mapped[list] = mapped_column(JSON, default=list)
    visual_evidence: Mapped[list] = mapped_column(JSON, default=list)
    contextual_notes: Mapped[str] = mapped_column(Text, default="")
    estimated_short_count: Mapped[int] = mapped_column(Integer, default=1)
    possible_hooks: Mapped[list] = mapped_column(JSON, default=list)
    editing_notes: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    scores: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    emergent_observations: Mapped[dict] = mapped_column(JSON, default=dict)
    weighted_score: Mapped[float] = mapped_column(Float, default=0)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_status: Mapped[str] = mapped_column(String, default="pending_review", index=True)
    processing_status: Mapped[str] = mapped_column(String, default="complete")
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    stream: Mapped[Stream] = relationship(back_populates="candidates")
    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="candidates")
    derived_assets: Mapped[list["DerivedAsset"]] = relationship(back_populates="candidate_window")


class DerivedAsset(Base):
    __tablename__ = "derived_assets"

    derived_asset_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("asset"))
    candidate_window_id: Mapped[str] = mapped_column(ForeignKey("candidate_windows.candidate_window_id"), index=True)
    asset_type: Mapped[str] = mapped_column(String)
    external_reference: Mapped[str] = mapped_column(String, default="")
    editor: Mapped[str] = mapped_column(String, default="")
    tool_used: Mapped[str] = mapped_column(String, default="")
    creation_status: Mapped[str] = mapped_column(String, default="planned")
    approval_status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    candidate_window: Mapped[CandidateWindow] = relationship(back_populates="derived_assets")
    publishing_records: Mapped[list["PublishingRecord"]] = relationship(back_populates="derived_asset")


class PublishingRecord(Base):
    __tablename__ = "publishing_records"

    publishing_record_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("pub"))
    derived_asset_id: Mapped[str] = mapped_column(ForeignKey("derived_assets.derived_asset_id"), index=True)
    platform: Mapped[str] = mapped_column(String)
    published_url: Mapped[str] = mapped_column(String, default="")
    published_at: Mapped[str] = mapped_column(String, default="")
    caption_or_title: Mapped[str] = mapped_column(Text, default="")
    campaign: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    derived_asset: Mapped[DerivedAsset] = relationship(back_populates="publishing_records")
    performance_records: Mapped[list["PerformanceRecord"]] = relationship(back_populates="publishing_record")


class PerformanceRecord(Base):
    __tablename__ = "performance_records"

    performance_record_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("perf"))
    publishing_record_id: Mapped[str] = mapped_column(ForeignKey("publishing_records.publishing_record_id"), index=True)
    measurement_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    views: Mapped[int] = mapped_column(Integer, default=0)
    watch_time: Mapped[int] = mapped_column(Integer, default=0)
    average_percentage_viewed: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    follows_attributed: Mapped[int] = mapped_column(Integer, default=0)
    conversions_or_sales: Mapped[int] = mapped_column(Integer, default=0)

    publishing_record: Mapped[PublishingRecord] = relationship(back_populates="performance_records")


class YouTubeOAuthCredential(Base):
    __tablename__ = "youtube_oauth_credentials"

    youtube_oauth_credential_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ytcred"))
    channel_id: Mapped[str] = mapped_column(String, default="", index=True)
    channel_title: Mapped[str] = mapped_column(String, default="")
    available_channels: Mapped[list] = mapped_column(JSON, default=list)
    granted_scopes: Mapped[list] = mapped_column(JSON, default=list)
    encrypted_token_json: Mapped[str] = mapped_column(Text, default="")
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connection_status: Mapped[str] = mapped_column(String, default="connected", index=True)
    reconnect_error: Mapped[str] = mapped_column(Text, default="")
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class YouTubeAnalyticsSync(Base):
    __tablename__ = "youtube_analytics_syncs"

    youtube_analytics_sync_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ytsync"))
    sync_mode: Mapped[str] = mapped_column(String, default="manual", index=True)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    start_date: Mapped[str] = mapped_column(String, default="")
    end_date: Mapped[str] = mapped_column(String, default="")
    rows_fetched: Mapped[int] = mapped_column(Integer, default=0)
    videos_updated: Mapped[int] = mapped_column(Integer, default=0)
    livestreams_updated: Mapped[int] = mapped_column(Integer, default=0)
    timeseries_points_updated: Mapped[int] = mapped_column(Integer, default=0)
    last_successful_date: Mapped[str] = mapped_column(String, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class YouTubeVideo(Base):
    __tablename__ = "youtube_videos"

    video_id: Mapped[str] = mapped_column(String, primary_key=True)
    stream_id: Mapped[str | None] = mapped_column(ForeignKey("streams.stream_id"), nullable=True, index=True)
    channel_id: Mapped[str] = mapped_column(String, default="", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[str] = mapped_column(String, default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail: Mapped[str] = mapped_column(String, default="")
    content_type: Mapped[str] = mapped_column(String, default="unknown", index=True)
    live_broadcast_content: Mapped[str] = mapped_column(String, default="")
    video_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    stream: Mapped[Stream | None] = relationship(back_populates="youtube_videos")
    daily_metrics: Mapped[list["YouTubeDailyMetric"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    livestream_metric: Mapped["YouTubeLivestreamMetric | None"] = relationship(back_populates="video", cascade="all, delete-orphan")
    livestream_timeseries: Mapped[list["YouTubeLivestreamTimeseries"]] = relationship(back_populates="video", cascade="all, delete-orphan")
    live_events: Mapped[list["YouTubeLiveEventPlaceholder"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class YouTubeDailyMetric(Base):
    __tablename__ = "youtube_daily_metrics"
    __table_args__ = (
        UniqueConstraint("date", "video_id", "content_type", "live_or_on_demand", name="uq_youtube_daily_metric"),
    )

    youtube_daily_metric_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ytdaily"))
    date: Mapped[str] = mapped_column(String, index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("youtube_videos.video_id"), index=True)
    content_type: Mapped[str] = mapped_column(String, default="unknown", index=True)
    live_or_on_demand: Mapped[str] = mapped_column(String, default="unknown", index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    engaged_views: Mapped[int] = mapped_column(Integer, default=0)
    watch_minutes: Mapped[float] = mapped_column(Float, default=0)
    avg_view_duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    subscribers_gained: Mapped[int] = mapped_column(Integer, default=0)
    subscribers_lost: Mapped[int] = mapped_column(Integer, default=0)
    estimated_revenue: Mapped[float] = mapped_column(Float, default=0)
    estimated_ad_revenue: Mapped[float] = mapped_column(Float, default=0)
    estimated_red_partner_revenue: Mapped[float] = mapped_column(Float, default=0)
    gross_revenue: Mapped[float] = mapped_column(Float, default=0)
    monetized_playbacks: Mapped[int] = mapped_column(Integer, default=0)
    ad_impressions: Mapped[int] = mapped_column(Integer, default=0)
    cpm: Mapped[float] = mapped_column(Float, default=0)
    playback_based_cpm: Mapped[float] = mapped_column(Float, default=0)
    other_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    video: Mapped[YouTubeVideo] = relationship(back_populates="daily_metrics")


class YouTubeLivestreamMetric(Base):
    __tablename__ = "youtube_livestream_metrics"

    video_id: Mapped[str] = mapped_column(ForeignKey("youtube_videos.video_id"), primary_key=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    live_views: Mapped[int] = mapped_column(Integer, default=0)
    replay_views: Mapped[int] = mapped_column(Integer, default=0)
    total_views: Mapped[int] = mapped_column(Integer, default=0)
    watch_minutes: Mapped[float] = mapped_column(Float, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    subscribers_gained: Mapped[int] = mapped_column(Integer, default=0)
    subscribers_lost: Mapped[int] = mapped_column(Integer, default=0)
    estimated_revenue: Mapped[float] = mapped_column(Float, default=0)
    average_concurrent_viewers: Mapped[float] = mapped_column(Float, default=0)
    peak_concurrent_viewers: Mapped[int] = mapped_column(Integer, default=0)
    other_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    video: Mapped[YouTubeVideo] = relationship(back_populates="livestream_metric")


class YouTubeLivestreamTimeseries(Base):
    __tablename__ = "youtube_livestream_timeseries"
    __table_args__ = (UniqueConstraint("video_id", "stream_position_seconds", name="uq_youtube_livestream_position"),)

    youtube_livestream_timeseries_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ytts"))
    video_id: Mapped[str] = mapped_column(ForeignKey("youtube_videos.video_id"), index=True)
    stream_position_seconds: Mapped[int] = mapped_column(Integer, index=True)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    concurrent_viewers: Mapped[float] = mapped_column(Float, default=0)
    average_concurrent_viewers: Mapped[float] = mapped_column(Float, default=0)
    peak_concurrent_viewers: Mapped[int] = mapped_column(Integer, default=0)
    other_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    video: Mapped[YouTubeVideo] = relationship(back_populates="livestream_timeseries")


class YouTubeLiveEventPlaceholder(Base):
    __tablename__ = "youtube_live_event_placeholders"

    youtube_live_event_placeholder_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ytevent"))
    video_id: Mapped[str] = mapped_column(ForeignKey("youtube_videos.video_id"), index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stream_position_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    amount_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    video: Mapped[YouTubeVideo] = relationship(back_populates="live_events")
