from dataclasses import replace
from datetime import date

from app.core.config import Settings
from app.models import YouTubeAnalyticsSync, YouTubeDailyMetric, YouTubeLivestreamMetric, YouTubeOAuthCredential, YouTubeVideo
from app.services.youtube_analytics import (
    analytics_overview,
    import_youtube_studio_csv,
    revenue_forecasts,
    select_analytics_channel,
    sync_start_date,
    sync_youtube_analytics,
)


class FakeAnalyticsApi:
    def __init__(self):
        self.daily_channel_id = ""
        self.timeseries_channel_id = ""

    def fetch_channels(self):
        return [
            {"id": "personal_channel", "title": "Chris Nielsen", "thumbnail": ""},
            {"id": "shocks_channel", "title": "Shock's Art", "thumbnail": ""},
        ]

    def fetch_daily_metrics(self, start_date, end_date, channel_id="MINE"):
        self.daily_channel_id = channel_id
        return [
            {
                "day": "2026-08-01",
                "video": "live_1",
                "creatorContentType": "LIVE_STREAM",
                "liveOrOnDemand": "LIVE",
                "views": 100,
                "engagedViews": 80,
                "estimatedMinutesWatched": 900,
                "averageViewDuration": 540,
                "likes": 12,
                "subscribersGained": 5,
                "subscribersLost": 1,
                "estimatedRevenue": 20.0,
                "estimatedAdRevenue": 18.0,
                "estimatedRedPartnerRevenue": 2.0,
                "grossRevenue": 22.0,
                "monetizedPlaybacks": 50,
                "adImpressions": 300,
                "cpm": 12.0,
                "playbackBasedCpm": 10.0,
            },
            {
                "day": "2026-08-02",
                "video": "live_1",
                "creatorContentType": "LIVE_STREAM",
                "liveOrOnDemand": "ON_DEMAND",
                "views": 50,
                "estimatedMinutesWatched": 200,
                "likes": 3,
                "estimatedRevenue": 5.0,
            },
            {
                "day": "2026-08-02",
                "video": "short_1",
                "creatorContentType": "SHORTS",
                "liveOrOnDemand": "ON_DEMAND",
                "views": 1000,
                "estimatedMinutesWatched": 100,
                "subscribersGained": 2,
                "estimatedRevenue": 1.0,
            },
        ]

    def fetch_video_metadata(self, video_ids):
        return [
            {
                "video_id": "live_1",
                "channel_id": "channel_1",
                "title": "Fixture Livestream",
                "published_at": "2026-08-01T12:00:00Z",
                "duration_seconds": 3600,
                "thumbnail": "https://example.test/live.jpg",
                "actual_start": "2026-08-01T12:00:00Z",
                "actual_end": "2026-08-01T13:00:00Z",
            },
            {
                "video_id": "short_1",
                "channel_id": "channel_1",
                "title": "Fixture Short",
                "published_at": "2026-08-02T12:00:00Z",
                "duration_seconds": 45,
                "thumbnail": "https://example.test/short.jpg",
            },
        ]

    def fetch_livestream_timeseries(self, video_ids, start_date, end_date, channel_id="MINE"):
        self.timeseries_channel_id = channel_id
        return [
            {"video": "live_1", "livestreamPosition": 0, "averageConcurrentViewers": 10, "peakConcurrentViewers": 12},
            {"video": "live_1", "livestreamPosition": 60, "averageConcurrentViewers": 20, "peakConcurrentViewers": 24},
        ]


def test_sync_youtube_analytics_persists_normalized_rows(db_session):
    result = sync_youtube_analytics(
        db_session,
        mode="initial",
        api=FakeAnalyticsApi(),
        settings=Settings(youtube_analytics_backfill_start="2026-08-01"),
        end_date=date(2026, 8, 2),
    )

    assert result.status == "complete"
    assert db_session.query(YouTubeVideo).count() == 2
    assert db_session.query(YouTubeDailyMetric).count() == 3
    live = db_session.get(YouTubeLivestreamMetric, "live_1")
    assert live.total_views == 150
    assert live.live_views == 100
    assert live.replay_views == 50
    assert live.peak_concurrent_viewers == 24


def test_analytics_overview_summarizes_content_types_and_forecasts(db_session):
    sync_youtube_analytics(
        db_session,
        mode="initial",
        api=FakeAnalyticsApi(),
        settings=Settings(youtube_analytics_backfill_start="2026-08-01"),
        end_date=date(2026, 8, 2),
    )

    overview = analytics_overview(db_session, start_date="2026-08-01", end_date="2026-08-02")
    assert overview["totals"]["views"] == 1150
    assert overview["totals"]["estimated_revenue"] == 26.0
    assert {row["content_type"] for row in overview["content_types"]} == {"live", "short"}
    assert overview["forecasts"]["mtd_run_rate"] is not None


def test_revenue_forecasts_use_pace_based_methods(db_session):
    sync_youtube_analytics(
        db_session,
        mode="initial",
        api=FakeAnalyticsApi(),
        settings=Settings(youtube_analytics_backfill_start="2026-08-01"),
        end_date=date(2026, 8, 2),
    )

    forecasts = revenue_forecasts(db_session, as_of=date(2026, 8, 2))
    assert forecasts["mtd_run_rate"] == 403.0
    assert forecasts["seven_day_pace"] is not None
    assert forecasts["twenty_eight_day_pace"] is not None


def test_incremental_sync_starts_three_days_before_last_success(db_session):
    sync = YouTubeAnalyticsSync(
        sync_mode="manual",
        status="complete",
        start_date="2026-08-01",
        end_date="2026-08-05",
        last_successful_date="2026-08-05",
    )
    db_session.add(sync)
    db_session.commit()

    start = sync_start_date(db_session, replace(Settings(), youtube_analytics_backfill_start="2026-06-01"), "manual")
    assert start.isoformat() == "2026-08-02"


def test_selected_managed_channel_is_used_for_sync(db_session):
    credential = YouTubeOAuthCredential(
        channel_id="personal_channel",
        channel_title="Chris Nielsen",
        available_channels=[
            {"id": "personal_channel", "title": "Chris Nielsen", "thumbnail": ""},
            {"id": "shocks_channel", "title": "Shock's Art", "thumbnail": ""},
        ],
        encrypted_token_json="unused",
        connection_status="connected",
    )
    db_session.add(credential)
    db_session.commit()
    selected = select_analytics_channel(db_session, "shocks_channel")
    fake_api = FakeAnalyticsApi()

    sync_youtube_analytics(
        db_session,
        mode="initial",
        api=fake_api,
        settings=Settings(youtube_analytics_backfill_start="2026-08-01"),
        end_date=date(2026, 8, 2),
    )

    assert selected.channel_title == "Shock's Art"
    assert fake_api.daily_channel_id == "shocks_channel"
    assert fake_api.timeseries_channel_id == "shocks_channel"


def test_analytics_route_renders_disconnected_state(client):
    response = client.get("/analytics")
    assert response.status_code == 200
    assert "Connect YouTube" in response.text
    assert "Analytics" in response.text


def test_livestream_detail_route_renders_synced_metrics(client, db_session):
    sync_youtube_analytics(
        db_session,
        mode="initial",
        api=FakeAnalyticsApi(),
        settings=Settings(youtube_analytics_backfill_start="2026-08-01"),
        end_date=date(2026, 8, 2),
    )

    response = client.get("/analytics/livestreams/live_1")
    assert response.status_code == 200
    assert "Fixture Livestream" in response.text
    assert "Concurrent Viewer Timeline" in response.text


def test_import_youtube_studio_daily_csv_populates_existing_analytics_tables(db_session):
    csv_content = """Date,Video ID,Video title,Content type,Live or on demand,Views,Engaged views,Watch time (hours),Average view duration,Likes,Subscribers gained,Subscribers lost,Estimated revenue
2026-08-03,csv_live_1,CSV Livestream,Live,Live,120,90,15,00:07:30,20,6,1,30.50
2026-08-04,csv_short_1,CSV Short,Shorts,On demand,1500,1000,4,00:00:09,80,10,0,2.25
"""

    result = import_youtube_studio_csv(db_session, csv_content, filename="daily.csv")
    overview = analytics_overview(db_session, start_date="2026-08-03", end_date="2026-08-04")

    assert result.status == "complete"
    assert result.rows_fetched == 2
    assert db_session.query(YouTubeDailyMetric).count() == 2
    assert overview["totals"]["views"] == 1620
    assert overview["totals"]["estimated_revenue"] == 32.75
    assert {row["content_type"] for row in overview["content_types"]} == {"live", "short"}


def test_import_youtube_studio_livestream_timeseries_csv(db_session):
    db_session.add(YouTubeVideo(video_id="csv_live_1", title="CSV Livestream", content_type="live", published_at="2026-08-03"))
    db_session.add(YouTubeLivestreamMetric(video_id="csv_live_1"))
    db_session.commit()
    csv_content = """Video ID,Livestream position,Average concurrent viewers,Peak concurrent viewers
csv_live_1,00:00:00,12,14
csv_live_1,00:01:00,20,25
"""

    result = import_youtube_studio_csv(db_session, csv_content, filename="timeline.csv", import_type="livestream_timeseries")
    live = db_session.get(YouTubeLivestreamMetric, "csv_live_1")

    assert result.status == "complete"
    assert result.timeseries_points_updated == 2
    assert live.average_concurrent_viewers == 16
    assert live.peak_concurrent_viewers == 25
