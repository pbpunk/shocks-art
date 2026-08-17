CREATE TABLE IF NOT EXISTS streams (
  stream_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  source_video_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  published_at TEXT NOT NULL DEFAULT '',
  duration INTEGER NOT NULL DEFAULT 0,
  thumbnail TEXT NOT NULL DEFAULT '',
  processing_status TEXT NOT NULL DEFAULT 'queued',
  schema_version TEXT NOT NULL DEFAULT '1.0',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_stream_source UNIQUE (platform, source_video_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  analysis_run_id TEXT PRIMARY KEY,
  stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  model TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '1.0',
  schema_version TEXT NOT NULL DEFAULT '1.0',
  request_started_at DATETIME NOT NULL,
  request_completed_at DATETIME,
  status TEXT NOT NULL DEFAULT 'queued',
  retry_count INTEGER NOT NULL DEFAULT 0,
  raw_response_location TEXT NOT NULL DEFAULT '',
  validation_errors JSON NOT NULL,
  exception_message TEXT NOT NULL DEFAULT '',
  usage JSON NOT NULL,
  estimated_cost FLOAT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_windows (
  candidate_window_id TEXT PRIMARY KEY,
  stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id),
  candidate_rank INTEGER NOT NULL DEFAULT 1,
  start_seconds INTEGER NOT NULL,
  end_seconds INTEGER NOT NULL,
  start_timestamp TEXT NOT NULL,
  end_timestamp TEXT NOT NULL,
  duration_seconds INTEGER NOT NULL,
  title TEXT NOT NULL,
  concise_summary TEXT NOT NULL,
  selection_reason TEXT NOT NULL,
  primary_pillar TEXT NOT NULL,
  secondary_pillars JSON NOT NULL,
  tags JSON NOT NULL,
  transcript_excerpt TEXT NOT NULL DEFAULT '',
  visual_description TEXT NOT NULL DEFAULT '',
  transcript_evidence JSON NOT NULL DEFAULT '[]',
  visual_evidence JSON NOT NULL DEFAULT '[]',
  contextual_notes TEXT NOT NULL DEFAULT '',
  estimated_short_count INTEGER NOT NULL DEFAULT 1,
  possible_hooks JSON NOT NULL,
  editing_notes JSON NOT NULL,
  risks JSON NOT NULL,
  scores JSON NOT NULL,
  confidence INTEGER NOT NULL DEFAULT 0,
  emergent_observations JSON NOT NULL,
  weighted_score FLOAT NOT NULL DEFAULT 0,
  review_status TEXT NOT NULL DEFAULT 'pending_review',
  processing_status TEXT NOT NULL DEFAULT 'complete',
  reviewer_notes TEXT NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_assets (
  derived_asset_id TEXT PRIMARY KEY,
  candidate_window_id TEXT NOT NULL REFERENCES candidate_windows(candidate_window_id),
  asset_type TEXT NOT NULL,
  external_reference TEXT NOT NULL DEFAULT '',
  editor TEXT NOT NULL DEFAULT '',
  tool_used TEXT NOT NULL DEFAULT '',
  creation_status TEXT NOT NULL DEFAULT 'planned',
  approval_status TEXT NOT NULL DEFAULT 'pending',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS publishing_records (
  publishing_record_id TEXT PRIMARY KEY,
  derived_asset_id TEXT NOT NULL REFERENCES derived_assets(derived_asset_id),
  platform TEXT NOT NULL,
  published_url TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  caption_or_title TEXT NOT NULL DEFAULT '',
  campaign TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_records (
  performance_record_id TEXT PRIMARY KEY,
  publishing_record_id TEXT NOT NULL REFERENCES publishing_records(publishing_record_id),
  measurement_timestamp DATETIME NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,
  watch_time INTEGER NOT NULL DEFAULT 0,
  average_percentage_viewed FLOAT NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  shares INTEGER NOT NULL DEFAULT 0,
  saves INTEGER NOT NULL DEFAULT 0,
  follows_attributed INTEGER NOT NULL DEFAULT 0,
  conversions_or_sales INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS youtube_oauth_credentials (
  youtube_oauth_credential_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL DEFAULT '',
  channel_title TEXT NOT NULL DEFAULT '',
  granted_scopes JSON NOT NULL,
  encrypted_token_json TEXT NOT NULL DEFAULT '',
  token_expiry DATETIME,
  connection_status TEXT NOT NULL DEFAULT 'connected',
  reconnect_error TEXT NOT NULL DEFAULT '',
  connected_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_analytics_syncs (
  youtube_analytics_sync_id TEXT PRIMARY KEY,
  sync_mode TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL DEFAULT 'queued',
  started_at DATETIME NOT NULL,
  completed_at DATETIME,
  start_date TEXT NOT NULL DEFAULT '',
  end_date TEXT NOT NULL DEFAULT '',
  rows_fetched INTEGER NOT NULL DEFAULT 0,
  videos_updated INTEGER NOT NULL DEFAULT 0,
  livestreams_updated INTEGER NOT NULL DEFAULT 0,
  timeseries_points_updated INTEGER NOT NULL DEFAULT 0,
  last_successful_date TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_videos (
  video_id TEXT PRIMARY KEY,
  stream_id TEXT REFERENCES streams(stream_id),
  channel_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  duration_seconds INTEGER NOT NULL DEFAULT 0,
  thumbnail TEXT NOT NULL DEFAULT '',
  content_type TEXT NOT NULL DEFAULT 'unknown',
  live_broadcast_content TEXT NOT NULL DEFAULT '',
  metadata JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_daily_metrics (
  youtube_daily_metric_id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  video_id TEXT NOT NULL REFERENCES youtube_videos(video_id),
  content_type TEXT NOT NULL DEFAULT 'unknown',
  live_or_on_demand TEXT NOT NULL DEFAULT 'unknown',
  views INTEGER NOT NULL DEFAULT 0,
  engaged_views INTEGER NOT NULL DEFAULT 0,
  watch_minutes FLOAT NOT NULL DEFAULT 0,
  avg_view_duration_seconds FLOAT NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  subscribers_gained INTEGER NOT NULL DEFAULT 0,
  subscribers_lost INTEGER NOT NULL DEFAULT 0,
  estimated_revenue FLOAT NOT NULL DEFAULT 0,
  estimated_ad_revenue FLOAT NOT NULL DEFAULT 0,
  estimated_red_partner_revenue FLOAT NOT NULL DEFAULT 0,
  gross_revenue FLOAT NOT NULL DEFAULT 0,
  monetized_playbacks INTEGER NOT NULL DEFAULT 0,
  ad_impressions INTEGER NOT NULL DEFAULT 0,
  cpm FLOAT NOT NULL DEFAULT 0,
  playback_based_cpm FLOAT NOT NULL DEFAULT 0,
  other_metrics JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_youtube_daily_metric UNIQUE (date, video_id, content_type, live_or_on_demand)
);

CREATE TABLE IF NOT EXISTS youtube_livestream_metrics (
  video_id TEXT PRIMARY KEY REFERENCES youtube_videos(video_id),
  scheduled_start DATETIME,
  actual_start DATETIME,
  actual_end DATETIME,
  duration_seconds INTEGER NOT NULL DEFAULT 0,
  live_views INTEGER NOT NULL DEFAULT 0,
  replay_views INTEGER NOT NULL DEFAULT 0,
  total_views INTEGER NOT NULL DEFAULT 0,
  watch_minutes FLOAT NOT NULL DEFAULT 0,
  likes INTEGER NOT NULL DEFAULT 0,
  subscribers_gained INTEGER NOT NULL DEFAULT 0,
  subscribers_lost INTEGER NOT NULL DEFAULT 0,
  estimated_revenue FLOAT NOT NULL DEFAULT 0,
  average_concurrent_viewers FLOAT NOT NULL DEFAULT 0,
  peak_concurrent_viewers INTEGER NOT NULL DEFAULT 0,
  other_metrics JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_livestream_timeseries (
  youtube_livestream_timeseries_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES youtube_videos(video_id),
  stream_position_seconds INTEGER NOT NULL,
  measured_at DATETIME,
  concurrent_viewers FLOAT NOT NULL DEFAULT 0,
  average_concurrent_viewers FLOAT NOT NULL DEFAULT 0,
  peak_concurrent_viewers INTEGER NOT NULL DEFAULT 0,
  other_metrics JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_youtube_livestream_position UNIQUE (video_id, stream_position_seconds)
);

CREATE TABLE IF NOT EXISTS youtube_live_event_placeholders (
  youtube_live_event_placeholder_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES youtube_videos(video_id),
  event_type TEXT NOT NULL,
  event_timestamp DATETIME,
  stream_position_seconds INTEGER,
  amount_micros INTEGER,
  currency TEXT NOT NULL DEFAULT '',
  event_metadata JSON NOT NULL,
  created_at DATETIME NOT NULL
);
