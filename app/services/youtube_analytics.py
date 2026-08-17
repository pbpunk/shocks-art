import csv
import base64
import json
import os
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from io import StringIO
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.config import ROOT_DIR, Settings, get_settings
from app.models import (
    Stream,
    YouTubeAnalyticsSync,
    YouTubeDailyMetric,
    YouTubeLivestreamMetric,
    YouTubeLivestreamTimeseries,
    YouTubeOAuthCredential,
    YouTubeVideo,
)
from app.services.youtube import parse_iso8601_duration


YOUTUBE_ANALYTICS_SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

STATE_PATH = ROOT_DIR / "data" / "youtube_oauth_state.json"


@dataclass
class SyncResult:
    sync_id: str
    status: str
    start_date: str
    end_date: str
    rows_fetched: int = 0
    videos_updated: int = 0
    livestreams_updated: int = 0
    timeseries_points_updated: int = 0
    error_message: str = ""


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def content_type_from_api(value: str, duration_seconds: int = 0, is_live: bool = False) -> str:
    normalized = (value or "").lower()
    if "short" in normalized:
        return "short"
    if is_live or "live" in normalized:
        return "live"
    if normalized in {"video_on_demand", "video on demand", "vod"}:
        return "vod"
    if duration_seconds and duration_seconds <= 60:
        return "short"
    return "vod" if duration_seconds else "unknown"


def normalize_live_or_on_demand(value: str) -> str:
    normalized = (value or "").lower().replace(" ", "_")
    if normalized in {"live", "subscribed_live"}:
        return "live"
    if normalized in {"on_demand", "ondemand", "replay"}:
        return "on_demand"
    return "unknown"


def revenue_per_thousand(estimated_revenue: float, views: int) -> float:
    return (estimated_revenue / views * 1000) if views else 0


def ensure_fernet_key(raw_key: str) -> str:
    if not raw_key:
        raise RuntimeError("YOUTUBE_TOKEN_ENCRYPTION_KEY is required before connecting YouTube Analytics.")
    try:
        base64.urlsafe_b64decode(raw_key.encode("utf-8"))
        return raw_key
    except Exception:
        digest = raw_key.encode("utf-8")[:32].ljust(32, b"0")
        return base64.urlsafe_b64encode(digest).decode("utf-8")


def encrypt_token_json(token_json: str, settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("cryptography is required for encrypted YouTube OAuth token storage.") from exc
    key = ensure_fernet_key(active_settings.youtube_token_encryption_key)
    return Fernet(key.encode("utf-8")).encrypt(token_json.encode("utf-8")).decode("utf-8")


def decrypt_token_json(encrypted_token_json: str, settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("cryptography is required for encrypted YouTube OAuth token storage.") from exc
    key = ensure_fernet_key(active_settings.youtube_token_encryption_key)
    return Fernet(key.encode("utf-8")).decrypt(encrypted_token_json.encode("utf-8")).decode("utf-8")


def connected_credential(db: Session) -> YouTubeOAuthCredential | None:
    return db.scalar(
        select(YouTubeOAuthCredential)
        .where(YouTubeOAuthCredential.connection_status == "connected")
        .order_by(YouTubeOAuthCredential.updated_at.desc())
    )


def youtube_connection_state(db: Session) -> dict[str, Any]:
    credential = connected_credential(db)
    latest_sync = db.scalar(select(YouTubeAnalyticsSync).order_by(YouTubeAnalyticsSync.started_at.desc()))
    return {
        "connected": credential is not None,
        "channel_title": credential.channel_title if credential else "",
        "channel_id": credential.channel_id if credential else "",
        "available_channels": credential.available_channels if credential else [],
        "token_expiry": credential.token_expiry if credential else None,
        "reconnect_error": credential.reconnect_error if credential else "",
        "latest_sync": sync_to_dict(latest_sync) if latest_sync else None,
    }


def sync_to_dict(sync: YouTubeAnalyticsSync) -> dict[str, Any]:
    return {
        "sync_id": sync.youtube_analytics_sync_id,
        "sync_mode": sync.sync_mode,
        "status": sync.status,
        "started_at": sync.started_at.isoformat() if sync.started_at else "",
        "completed_at": sync.completed_at.isoformat() if sync.completed_at else "",
        "start_date": sync.start_date,
        "end_date": sync.end_date,
        "rows_fetched": sync.rows_fetched,
        "videos_updated": sync.videos_updated,
        "livestreams_updated": sync.livestreams_updated,
        "timeseries_points_updated": sync.timeseries_points_updated,
        "last_successful_date": sync.last_successful_date,
        "error_message": sync.error_message,
    }


def build_oauth_authorization_url(settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    if not active_settings.youtube_oauth_client_secrets_file:
        raise RuntimeError("YOUTUBE_OAUTH_CLIENT_SECRETS_FILE is required before connecting YouTube Analytics.")
    if not active_settings.youtube_token_encryption_key:
        raise RuntimeError("YOUTUBE_TOKEN_ENCRYPTION_KEY is required before connecting YouTube Analytics.")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError("google-auth-oauthlib is required for YouTube OAuth.") from exc
    allow_local_http_oauth(active_settings.youtube_oauth_redirect_uri)
    flow = Flow.from_client_secrets_file(
        active_settings.youtube_oauth_client_secrets_file,
        scopes=YOUTUBE_ANALYTICS_SCOPES,
        redirect_uri=active_settings.youtube_oauth_redirect_uri,
    )
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
    )
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": getattr(flow, "code_verifier", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return authorization_url


def exchange_oauth_callback(db: Session, authorization_response: str, state: str | None, settings: Settings | None = None) -> YouTubeOAuthCredential:
    active_settings = settings or get_settings()
    expected_state = ""
    code_verifier = ""
    if STATE_PATH.exists():
        oauth_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        expected_state = oauth_state.get("state", "")
        code_verifier = oauth_state.get("code_verifier", "")
    if expected_state and state != expected_state:
        raise RuntimeError("YouTube OAuth state did not match. Start the connection again.")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError("google-auth-oauthlib is required for YouTube OAuth.") from exc
    allow_local_http_oauth(active_settings.youtube_oauth_redirect_uri)
    flow = Flow.from_client_secrets_file(
        active_settings.youtube_oauth_client_secrets_file,
        scopes=YOUTUBE_ANALYTICS_SCOPES,
        redirect_uri=active_settings.youtube_oauth_redirect_uri,
    )
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorization_response)
    api = RealYouTubeAnalyticsApi(flow.credentials)
    channels = api.fetch_channels()
    if not channels:
        raise RuntimeError("No YouTube channel is available for the connected account.")
    channel = channels[0]
    token_json = flow.credentials.to_json()
    credential = connected_credential(db) or YouTubeOAuthCredential()
    credential.channel_id = channel.get("id", "")
    credential.channel_title = channel.get("title", "")
    credential.available_channels = channels
    credential.granted_scopes = list(flow.credentials.scopes or YOUTUBE_ANALYTICS_SCOPES)
    credential.encrypted_token_json = encrypt_token_json(token_json, active_settings)
    credential.token_expiry = flow.credentials.expiry
    credential.connection_status = "connected"
    credential.reconnect_error = ""
    db.add(credential)
    db.commit()
    return credential


def allow_local_http_oauth(redirect_uri: str) -> None:
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def credentials_from_record(record: YouTubeOAuthCredential, settings: Settings | None = None):
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("google-auth is required for stored YouTube OAuth credentials.") from exc
    token_info = json.loads(decrypt_token_json(record.encrypted_token_json, settings))
    return Credentials.from_authorized_user_info(token_info, scopes=YOUTUBE_ANALYTICS_SCOPES)


class RealYouTubeAnalyticsApi:
    def __init__(self, credentials) -> None:
        from googleapiclient.discovery import build

        self.analytics = build("youtubeAnalytics", "v2", credentials=credentials)
        self.youtube = build("youtube", "v3", credentials=credentials)

    def fetch_channels(self) -> list[dict[str, str]]:
        channels: list[dict[str, str]] = []
        page_token: str | None = None
        while True:
            response = (
                self.youtube.channels()
                .list(part="id,snippet", mine=True, maxResults=50, pageToken=page_token)
                .execute()
            )
            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                thumbnails = snippet.get("thumbnails", {})
                thumbnail = thumbnails.get("default") or thumbnails.get("medium") or {}
                channels.append(
                    {
                        "id": item.get("id", ""),
                        "title": snippet.get("title", ""),
                        "thumbnail": thumbnail.get("url", ""),
                    }
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                return channels

    def fetch_daily_metrics(self, start_date: str, end_date: str, channel_id: str = "MINE") -> list[dict[str, Any]]:
        metrics = ",".join(
            [
                "views",
                "engagedViews",
                "estimatedMinutesWatched",
                "averageViewDuration",
                "likes",
                "subscribersGained",
                "subscribersLost",
                "estimatedRevenue",
                "estimatedAdRevenue",
                "estimatedRedPartnerRevenue",
                "grossRevenue",
                "monetizedPlaybacks",
                "adImpressions",
                "cpm",
                "playbackBasedCpm",
            ]
        )
        response = (
            self.analytics.reports()
                .query(
                ids=f"channel=={channel_id or 'MINE'}",
                startDate=start_date,
                endDate=end_date,
                dimensions="day,video,creatorContentType,liveOrOnDemand",
                metrics=metrics,
                maxResults=200000,
                sort="day",
            )
            .execute()
        )
        return rows_to_dicts(response)

    def fetch_livestream_timeseries(self, video_ids: list[str], start_date: str, end_date: str, channel_id: str = "MINE") -> list[dict[str, Any]]:
        if not video_ids:
            return []
        rows: list[dict[str, Any]] = []
        for video_id in video_ids:
            response = (
                self.analytics.reports()
                .query(
                    ids=f"channel=={channel_id or 'MINE'}",
                    startDate=start_date,
                    endDate=end_date,
                    dimensions="video,livestreamPosition",
                    filters=f"video=={video_id}",
                    metrics="averageConcurrentViewers,peakConcurrentViewers",
                    maxResults=200000,
                    sort="livestreamPosition",
                )
                .execute()
            )
            rows.extend(rows_to_dicts(response))
        return rows

    def fetch_video_metadata(self, video_ids: list[str]) -> list[dict[str, Any]]:
        videos: list[dict[str, Any]] = []
        for start in range(0, len(video_ids), 50):
            chunk = video_ids[start:start + 50]
            response = (
                self.youtube.videos()
                .list(part="snippet,contentDetails,liveStreamingDetails", id=",".join(chunk), maxResults=50)
                .execute()
            )
            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                thumbnails = snippet.get("thumbnails", {})
                thumbnail = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
                live_details = item.get("liveStreamingDetails", {})
                duration_seconds = parse_iso8601_duration(item.get("contentDetails", {}).get("duration", ""))
                videos.append(
                    {
                        "video_id": item.get("id", ""),
                        "channel_id": snippet.get("channelId", ""),
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "duration_seconds": duration_seconds,
                        "thumbnail": thumbnail.get("url", ""),
                        "live_broadcast_content": snippet.get("liveBroadcastContent", ""),
                        "scheduled_start": live_details.get("scheduledStartTime"),
                        "actual_start": live_details.get("actualStartTime"),
                        "actual_end": live_details.get("actualEndTime"),
                        "metadata": {"liveStreamingDetails": live_details},
                    }
                )
        return videos


def rows_to_dicts(response: dict[str, Any]) -> list[dict[str, Any]]:
    headers = [column.get("name", "") for column in response.get("columnHeaders", [])]
    return [dict(zip(headers, row, strict=False)) for row in response.get("rows", [])]


def import_youtube_studio_csv(db: Session, csv_content: str, filename: str = "", import_type: str = "daily") -> SyncResult:
    sync = YouTubeAnalyticsSync(sync_mode=f"csv_{import_type}", status="processing")
    db.add(sync)
    db.flush()
    try:
        rows = read_csv_rows(csv_content)
        normalized = normalize_csv_rows(rows, import_type)
        dates = [row["day"] for row in normalized["daily"] if row.get("day")]
        sync.start_date = min(dates) if dates else ""
        sync.end_date = max(dates) if dates else ""
        sync.rows_fetched = len(rows)
        video_ids: set[str] = set()
        livestream_ids: set[str] = set()
        timeseries_count = 0
        for row in normalized["videos"]:
            upsert_video(db, row)
            video_ids.add(row["video_id"])
            if row.get("content_type") == "live" or row.get("actual_start") or row.get("scheduled_start"):
                upsert_livestream_metadata(db, row)
                livestream_ids.add(row["video_id"])
        db.flush()
        for row in normalized["daily"]:
            video_id = row.get("video")
            if not video_id:
                continue
            if not db.get(YouTubeVideo, video_id):
                upsert_video(
                    db,
                    {
                        "video_id": video_id,
                        "title": row.get("title", ""),
                        "published_at": row.get("day", ""),
                        "content_type": content_type_from_api(row.get("creatorContentType", "")),
                    },
                )
            upsert_daily_metric(db, row, {"content_type": row.get("creatorContentType", "")})
            video_ids.add(video_id)
            if content_type_from_api(row.get("creatorContentType", "")) == "live":
                livestream_ids.add(video_id)
        for row in normalized["timeseries"]:
            if row.get("video") and not db.get(YouTubeVideo, row["video"]):
                upsert_video(db, {"video_id": row["video"], "content_type": "live"})
            if upsert_livestream_timeseries(db, row):
                timeseries_count += 1
            if row.get("video"):
                livestream_ids.add(row["video"])
        db.flush()
        for video_id in livestream_ids:
            aggregate_livestream_metric(db, video_id)
        sync.videos_updated = len(video_ids)
        sync.livestreams_updated = len(livestream_ids)
        sync.timeseries_points_updated = timeseries_count
        sync.status = "complete"
        sync.completed_at = datetime.now(timezone.utc)
        sync.last_successful_date = sync.end_date
        if not normalized["daily"] and not normalized["timeseries"]:
            sync.error_message = "CSV imported but no recognized analytics rows were found."
        db.commit()
        return SyncResult(
            sync.youtube_analytics_sync_id,
            sync.status,
            sync.start_date,
            sync.end_date,
            sync.rows_fetched,
            sync.videos_updated,
            sync.livestreams_updated,
            sync.timeseries_points_updated,
            sync.error_message,
        )
    except Exception as exc:
        sync.status = "failed"
        sync.completed_at = datetime.now(timezone.utc)
        sync.error_message = str(exc)[:1000]
        db.commit()
        return SyncResult(sync.youtube_analytics_sync_id, sync.status, sync.start_date, sync.end_date, error_message=sync.error_message)


def read_csv_rows(csv_content: str) -> list[dict[str, str]]:
    text = csv_content.lstrip("\ufeff")
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("The uploaded CSV does not have a header row.")
    return [{normalize_csv_header(key): value for key, value in row.items() if key is not None} for row in reader]


def normalize_csv_rows(rows: list[dict[str, str]], import_type: str) -> dict[str, list[dict[str, Any]]]:
    daily: list[dict[str, Any]] = []
    videos: dict[str, dict[str, Any]] = {}
    timeseries: list[dict[str, Any]] = []
    for source in rows:
        video_id = csv_value(source, "video_id", "video", "content")
        date_value = normalize_csv_date(csv_value(source, "date", "day"))
        content_type = csv_content_type(source)
        title = csv_value(source, "video_title", "title", "content_title")
        if video_id:
            videos.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "title": title,
                    "published_at": date_value,
                    "duration_seconds": duration_to_seconds(csv_value(source, "duration")),
                    "thumbnail": csv_value(source, "thumbnail", "thumbnail_url"),
                    "content_type": content_type,
                    "actual_start": csv_datetime_value(source, "actual_start_time", "actual_start"),
                    "actual_end": csv_datetime_value(source, "actual_end_time", "actual_end"),
                    "scheduled_start": csv_datetime_value(source, "scheduled_start_time", "scheduled_start"),
                },
            )
            if title:
                videos[video_id]["title"] = title
            if content_type != "unknown":
                videos[video_id]["content_type"] = content_type
        position = stream_position_seconds(source)
        if import_type == "livestream_timeseries" or position is not None:
            if video_id and position is not None:
                timeseries.append(
                    {
                        "video": video_id,
                        "livestreamPosition": position,
                        "averageConcurrentViewers": csv_number(source, "average_concurrent_viewers", "avg_concurrent_viewers", "concurrent_viewers"),
                        "peakConcurrentViewers": csv_number(source, "peak_concurrent_viewers", "peak_concurrents"),
                    }
                )
            continue
        if date_value and video_id:
            daily.append(
                {
                    "day": date_value,
                    "video": video_id,
                    "title": title,
                    "creatorContentType": content_type,
                    "liveOrOnDemand": csv_live_or_on_demand(source),
                    "views": csv_number(source, "views"),
                    "engagedViews": csv_number(source, "engaged_views"),
                    "estimatedMinutesWatched": csv_watch_minutes(source),
                    "averageViewDuration": duration_to_seconds(csv_value(source, "average_view_duration", "avg_view_duration")),
                    "likes": csv_number(source, "likes"),
                    "subscribersGained": csv_number(source, "subscribers_gained", "subscribers"),
                    "subscribersLost": csv_number(source, "subscribers_lost"),
                    "estimatedRevenue": csv_number(source, "estimated_revenue", "revenue"),
                    "estimatedAdRevenue": csv_number(source, "estimated_ad_revenue", "ad_revenue"),
                    "estimatedRedPartnerRevenue": csv_number(source, "estimated_red_partner_revenue", "youtube_premium_revenue"),
                    "grossRevenue": csv_number(source, "gross_revenue"),
                    "monetizedPlaybacks": csv_number(source, "monetized_playbacks"),
                    "adImpressions": csv_number(source, "ad_impressions"),
                    "cpm": csv_number(source, "cpm"),
                    "playbackBasedCpm": csv_number(source, "playback_based_cpm", "playback_based_cpm_"),
                    "csvSource": "youtube_studio",
                }
            )
    return {"daily": daily, "videos": list(videos.values()), "timeseries": timeseries}


def normalize_csv_header(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("\ufeff", "")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "pct")
        .replace("$", "")
        .replace("/", "_")
        .replace("-", "_")
        .replace(" ", "_")
    )


def csv_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def csv_number(row: dict[str, str], *keys: str) -> float:
    raw = csv_value(row, *keys).replace("$", "").replace(",", "").replace("%", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        return float(raw) if raw else 0
    except ValueError:
        return 0


def normalize_csv_date(value: str) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    parsed = parse_datetime(value)
    return parsed.date().isoformat() if parsed else ""


def csv_datetime_value(row: dict[str, str], *keys: str) -> str:
    value = csv_value(row, *keys)
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed else value


def csv_content_type(row: dict[str, str]) -> str:
    value = csv_value(row, "content_type", "creator_content_type", "format")
    return content_type_from_api(value, duration_to_seconds(csv_value(row, "duration")), "live" in value.lower())


def csv_live_or_on_demand(row: dict[str, str]) -> str:
    value = csv_value(row, "live_or_on_demand", "live_or_on_demand_", "traffic_source_detail", "playback_type")
    if not value and csv_content_type(row) == "live":
        value = "unknown"
    return value


def csv_watch_minutes(row: dict[str, str]) -> float:
    minutes = csv_number(row, "watch_time_minutes", "watch_time_minutes_", "estimated_minutes_watched")
    if minutes:
        return minutes
    hours = csv_number(row, "watch_time_hours", "watch_time_hours_")
    return hours * 60 if hours else csv_number(row, "watch_time")


def duration_to_seconds(value: str) -> int:
    if not value:
        return 0
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if len(parts) in {2, 3} and all(part.replace(".", "", 1).isdigit() for part in parts):
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(float(part))
        return seconds
    return 0


def stream_position_seconds(row: dict[str, str]) -> int | None:
    raw = csv_value(row, "livestream_position", "stream_position", "elapsed_time", "time")
    if not raw:
        return None
    seconds = duration_to_seconds(raw)
    return seconds if seconds or raw in {"0", "00:00", "00:00:00"} else None


def analytics_api_from_db(db: Session, settings: Settings | None = None) -> RealYouTubeAnalyticsApi:
    credential = connected_credential(db)
    if not credential:
        raise RuntimeError("Connect YouTube Analytics before syncing.")
    try:
        creds = credentials_from_record(credential, settings)
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            credential.encrypted_token_json = encrypt_token_json(creds.to_json(), settings)
            credential.token_expiry = creds.expiry
            credential.reconnect_error = ""
            db.commit()
        return RealYouTubeAnalyticsApi(creds)
    except Exception as exc:
        credential.connection_status = "needs_reconnect"
        credential.reconnect_error = str(exc)[:1000]
        db.commit()
        raise


def refresh_available_channels(db: Session, api: Any | None = None, settings: Settings | None = None) -> list[dict[str, str]]:
    credential = connected_credential(db)
    if not credential:
        raise RuntimeError("Connect YouTube Analytics before choosing a channel.")
    active_api = api or analytics_api_from_db(db, settings)
    channels = active_api.fetch_channels()
    credential.available_channels = channels
    if channels and not any(channel.get("id") == credential.channel_id for channel in channels):
        credential.channel_id = channels[0].get("id", "")
        credential.channel_title = channels[0].get("title", "")
    db.commit()
    return channels


def select_analytics_channel(db: Session, channel_id: str) -> YouTubeOAuthCredential:
    credential = connected_credential(db)
    if not credential:
        raise RuntimeError("Connect YouTube Analytics before choosing a channel.")
    match = next((channel for channel in credential.available_channels if channel.get("id") == channel_id), None)
    if not match:
        raise RuntimeError("That channel is not available for the connected Google account. Refresh channels and try again.")
    credential.channel_id = match.get("id", "")
    credential.channel_title = match.get("title", "")
    credential.reconnect_error = ""
    db.commit()
    return credential


def sync_youtube_analytics(
    db: Session,
    mode: str = "manual",
    api: Any | None = None,
    settings: Settings | None = None,
    end_date: date | None = None,
) -> SyncResult:
    active_settings = settings or get_settings()
    sync = YouTubeAnalyticsSync(sync_mode=mode, status="processing")
    db.add(sync)
    db.flush()
    try:
        start = sync_start_date(db, active_settings, mode)
        end = end_date or today_utc()
        sync.start_date = start.isoformat()
        sync.end_date = end.isoformat()
        if start > end:
            sync.status = "complete"
            sync.completed_at = datetime.now(timezone.utc)
            db.commit()
            return SyncResult(sync.youtube_analytics_sync_id, sync.status, sync.start_date, sync.end_date)
        active_api = api or analytics_api_from_db(db, active_settings)
        selected_channel_id = selected_analytics_channel_id(db)
        daily_rows = active_api.fetch_daily_metrics(sync.start_date, sync.end_date, selected_channel_id)
        sync.rows_fetched += len(daily_rows)
        video_ids = sorted({str(row.get("video", "")) for row in daily_rows if row.get("video")})
        metadata_rows = active_api.fetch_video_metadata(video_ids) if video_ids else []
        metadata_by_video_id = {row["video_id"]: row for row in metadata_rows}
        for row in metadata_rows:
            upsert_video(db, row)
            if row.get("actual_start") or row.get("scheduled_start"):
                upsert_livestream_metadata(db, row)
        sync.videos_updated = len(metadata_rows)
        db.flush()
        for row in daily_rows:
            video_id = str(row.get("video", ""))
            metadata = metadata_by_video_id.get(video_id, {})
            if video_id and not db.get(YouTubeVideo, video_id):
                upsert_video(db, {"video_id": video_id, "content_type": content_type_from_api(row.get("creatorContentType", ""))})
            upsert_daily_metric(db, row, metadata)
        db.flush()
        live_video_ids = sorted(
            {
                row["video_id"]
                for row in metadata_rows
                if row.get("actual_start")
                or row.get("scheduled_start")
                or row.get("content_type") == "live"
                or row.get("live_broadcast_content") in {"live", "upcoming"}
            }
        )
        for video_id in live_video_ids:
            aggregate_livestream_metric(db, video_id)
        sync.livestreams_updated = len(live_video_ids)
        timeseries_rows = active_api.fetch_livestream_timeseries(live_video_ids, sync.start_date, sync.end_date, selected_channel_id)
        sync.rows_fetched += len(timeseries_rows)
        for row in timeseries_rows:
            if upsert_livestream_timeseries(db, row):
                sync.timeseries_points_updated += 1
        db.flush()
        for video_id in live_video_ids:
            aggregate_livestream_metric(db, video_id)
        sync.status = "complete"
        sync.completed_at = datetime.now(timezone.utc)
        sync.last_successful_date = sync.end_date
        db.commit()
        db.expire_all()
        return SyncResult(
            sync.youtube_analytics_sync_id,
            sync.status,
            sync.start_date,
            sync.end_date,
            sync.rows_fetched,
            sync.videos_updated,
            sync.livestreams_updated,
            sync.timeseries_points_updated,
        )
    except Exception as exc:
        sync.status = "failed"
        sync.completed_at = datetime.now(timezone.utc)
        sync.error_message = str(exc)[:1000]
        db.commit()
        return SyncResult(sync.youtube_analytics_sync_id, sync.status, sync.start_date, sync.end_date, error_message=sync.error_message)


def sync_start_date(db: Session, settings: Settings, mode: str) -> date:
    if mode == "initial":
        return parse_date(settings.youtube_analytics_backfill_start)
    latest_success = db.scalar(
        select(YouTubeAnalyticsSync)
        .where(YouTubeAnalyticsSync.status == "complete", YouTubeAnalyticsSync.last_successful_date != "")
        .order_by(YouTubeAnalyticsSync.completed_at.desc())
    )
    if not latest_success:
        return parse_date(settings.youtube_analytics_backfill_start)
    return max(parse_date(settings.youtube_analytics_backfill_start), parse_date(latest_success.last_successful_date) - timedelta(days=3))


def selected_analytics_channel_id(db: Session) -> str:
    credential = connected_credential(db)
    return credential.channel_id if credential and credential.channel_id else "MINE"


def upsert_video(db: Session, row: dict[str, Any]) -> YouTubeVideo:
    video_id = str(row.get("video_id") or row.get("video") or "")
    video = db.get(YouTubeVideo, video_id) if video_id else None
    if not video:
        video = YouTubeVideo(video_id=video_id)
        db.add(video)
    stream = db.scalar(select(Stream).where(Stream.platform == "youtube", Stream.source_video_id == video_id))
    duration_seconds = int(float(row.get("duration_seconds") or video.duration_seconds or 0))
    is_live = bool(row.get("actual_start") or row.get("scheduled_start") or row.get("live_broadcast_content") in {"live", "upcoming"})
    video.stream_id = stream.stream_id if stream else video.stream_id
    video.channel_id = row.get("channel_id", video.channel_id)
    video.title = row.get("title", video.title)
    video.description = row.get("description", video.description)
    video.published_at = row.get("published_at", video.published_at)
    video.duration_seconds = duration_seconds
    video.thumbnail = row.get("thumbnail", video.thumbnail)
    video.live_broadcast_content = row.get("live_broadcast_content", video.live_broadcast_content)
    video.content_type = row.get("content_type") or content_type_from_api(row.get("creatorContentType", ""), duration_seconds, is_live)
    video.video_metadata = row.get("metadata", video.video_metadata or {})
    return video


def upsert_daily_metric(db: Session, row: dict[str, Any], metadata: dict[str, Any] | None = None) -> YouTubeDailyMetric:
    metadata = metadata or {}
    video_id = str(row.get("video", ""))
    content_type = content_type_from_api(
        str(row.get("creatorContentType", metadata.get("content_type", ""))),
        int(metadata.get("duration_seconds") or 0),
        bool(metadata.get("actual_start") or metadata.get("scheduled_start")),
    )
    live_or_on_demand = normalize_live_or_on_demand(str(row.get("liveOrOnDemand", "")))
    metric = db.scalar(
        select(YouTubeDailyMetric).where(
            and_(
                YouTubeDailyMetric.date == str(row.get("day", "")),
                YouTubeDailyMetric.video_id == video_id,
                YouTubeDailyMetric.content_type == content_type,
                YouTubeDailyMetric.live_or_on_demand == live_or_on_demand,
            )
        )
    )
    if not metric:
        metric = YouTubeDailyMetric(date=str(row.get("day", "")), video_id=video_id, content_type=content_type, live_or_on_demand=live_or_on_demand)
        db.add(metric)
    metric.views = int_metric(row, "views")
    metric.engaged_views = int_metric(row, "engagedViews")
    metric.watch_minutes = float_metric(row, "estimatedMinutesWatched")
    metric.avg_view_duration_seconds = float_metric(row, "averageViewDuration")
    metric.likes = int_metric(row, "likes")
    metric.subscribers_gained = int_metric(row, "subscribersGained")
    metric.subscribers_lost = int_metric(row, "subscribersLost")
    metric.estimated_revenue = float_metric(row, "estimatedRevenue")
    metric.estimated_ad_revenue = float_metric(row, "estimatedAdRevenue")
    metric.estimated_red_partner_revenue = float_metric(row, "estimatedRedPartnerRevenue")
    metric.gross_revenue = float_metric(row, "grossRevenue")
    metric.monetized_playbacks = int_metric(row, "monetizedPlaybacks")
    metric.ad_impressions = int_metric(row, "adImpressions")
    metric.cpm = float_metric(row, "cpm")
    metric.playback_based_cpm = float_metric(row, "playbackBasedCpm")
    known = {
        "day", "video", "creatorContentType", "liveOrOnDemand", "views", "engagedViews",
        "estimatedMinutesWatched", "averageViewDuration", "likes", "subscribersGained",
        "subscribersLost", "estimatedRevenue", "estimatedAdRevenue", "estimatedRedPartnerRevenue",
        "grossRevenue", "monetizedPlaybacks", "adImpressions", "cpm", "playbackBasedCpm",
    }
    metric.other_metrics = {key: value for key, value in row.items() if key not in known}
    return metric


def upsert_livestream_metadata(db: Session, row: dict[str, Any]) -> YouTubeLivestreamMetric:
    video_id = row["video_id"]
    metric = db.get(YouTubeLivestreamMetric, video_id)
    if not metric:
        metric = YouTubeLivestreamMetric(video_id=video_id)
        db.add(metric)
    metric.scheduled_start = parse_datetime(row.get("scheduled_start"))
    metric.actual_start = parse_datetime(row.get("actual_start"))
    metric.actual_end = parse_datetime(row.get("actual_end"))
    if metric.actual_start and metric.actual_end:
        metric.duration_seconds = max(0, int((metric.actual_end - metric.actual_start).total_seconds()))
    return metric


def upsert_livestream_timeseries(db: Session, row: dict[str, Any]) -> bool:
    video_id = str(row.get("video", ""))
    if not video_id:
        return False
    position = int_metric(row, "livestreamPosition")
    existing = db.scalar(
        select(YouTubeLivestreamTimeseries).where(
            YouTubeLivestreamTimeseries.video_id == video_id,
            YouTubeLivestreamTimeseries.stream_position_seconds == position,
        )
    )
    created = existing is None
    metric = existing or YouTubeLivestreamTimeseries(video_id=video_id, stream_position_seconds=position)
    live_metric = db.get(YouTubeLivestreamMetric, video_id)
    if live_metric and live_metric.actual_start:
        metric.measured_at = live_metric.actual_start + timedelta(seconds=position)
    metric.average_concurrent_viewers = float_metric(row, "averageConcurrentViewers")
    metric.peak_concurrent_viewers = int_metric(row, "peakConcurrentViewers")
    metric.concurrent_viewers = metric.average_concurrent_viewers or float(metric.peak_concurrent_viewers)
    known = {"video", "livestreamPosition", "averageConcurrentViewers", "peakConcurrentViewers"}
    metric.other_metrics = {key: value for key, value in row.items() if key not in known}
    db.add(metric)
    return created


def aggregate_livestream_metric(db: Session, video_id: str) -> None:
    metric = db.get(YouTubeLivestreamMetric, video_id)
    if not metric:
        metric = YouTubeLivestreamMetric(video_id=video_id)
        db.add(metric)
    daily = list(db.scalars(select(YouTubeDailyMetric).where(YouTubeDailyMetric.video_id == video_id)).all())
    metric.live_views = sum(item.views for item in daily if item.live_or_on_demand == "live")
    metric.replay_views = sum(item.views for item in daily if item.live_or_on_demand == "on_demand")
    metric.total_views = sum(item.views for item in daily)
    metric.watch_minutes = sum(item.watch_minutes for item in daily)
    metric.likes = sum(item.likes for item in daily)
    metric.subscribers_gained = sum(item.subscribers_gained for item in daily)
    metric.subscribers_lost = sum(item.subscribers_lost for item in daily)
    metric.estimated_revenue = sum(item.estimated_revenue for item in daily)
    ts_stats = db.execute(
        select(
            func.avg(YouTubeLivestreamTimeseries.concurrent_viewers),
            func.max(YouTubeLivestreamTimeseries.peak_concurrent_viewers),
        ).where(YouTubeLivestreamTimeseries.video_id == video_id)
    ).one()
    metric.average_concurrent_viewers = float(ts_stats[0] or 0)
    metric.peak_concurrent_viewers = int(ts_stats[1] or 0)


def int_metric(row: dict[str, Any], key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def float_metric(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def analytics_overview(db: Session, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    end = parse_date(end_date) if end_date else today_utc()
    start = parse_date(start_date) if start_date else end.replace(day=1)
    previous_month_end = start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    rows = metrics_between(db, start, end)
    previous_rows = metrics_between(db, previous_month_start, previous_month_end)
    totals = summarize_daily_rows(rows)
    previous = summarize_daily_rows(previous_rows)
    charts = daily_chart_rows(rows)
    content_types = content_type_breakdown(rows)
    forecasts = revenue_forecasts(db, end)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "totals": totals,
        "previous_month": previous,
        "deltas": delta_summary(totals, previous),
        "charts": charts,
        "content_types": content_types,
        "forecasts": forecasts,
        "livestreams": livestream_summaries(db, start, end),
        "connection": youtube_connection_state(db),
    }


def metrics_between(db: Session, start: date, end: date) -> list[YouTubeDailyMetric]:
    return list(
        db.scalars(
            select(YouTubeDailyMetric)
            .where(YouTubeDailyMetric.date >= start.isoformat(), YouTubeDailyMetric.date <= end.isoformat())
            .order_by(YouTubeDailyMetric.date)
        )
    )


def summarize_daily_rows(rows: list[YouTubeDailyMetric]) -> dict[str, float]:
    views = sum(row.views for row in rows)
    revenue = sum(row.estimated_revenue for row in rows)
    subs_gained = sum(row.subscribers_gained for row in rows)
    subs_lost = sum(row.subscribers_lost for row in rows)
    watch_minutes = sum(row.watch_minutes for row in rows)
    return {
        "estimated_revenue": revenue,
        "views": views,
        "watch_minutes": watch_minutes,
        "watch_hours": watch_minutes / 60,
        "likes": sum(row.likes for row in rows),
        "subscribers_gained": subs_gained,
        "subscribers_lost": subs_lost,
        "subscriber_net": subs_gained - subs_lost,
        "rpm": revenue_per_thousand(revenue, views),
    }


def delta_summary(current: dict[str, float], previous: dict[str, float]) -> dict[str, float | None]:
    keys = ["estimated_revenue", "views", "watch_minutes", "subscribers_gained", "subscribers_lost", "rpm"]
    return {key: percent_delta(current[key], previous[key]) for key in keys}


def percent_delta(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return (current - previous) / previous * 100


def daily_chart_rows(rows: list[YouTubeDailyMetric]) -> list[dict[str, float | str]]:
    grouped: dict[str, list[YouTubeDailyMetric]] = {}
    for row in rows:
        grouped.setdefault(row.date, []).append(row)
    return [
        {
            "date": day,
            "revenue": summary["estimated_revenue"],
            "views": summary["views"],
            "subscriber_net": summary["subscriber_net"],
            "watch_minutes": summary["watch_minutes"],
        }
        for day, group_rows in sorted(grouped.items())
        for summary in [summarize_daily_rows(group_rows)]
    ]


def content_type_breakdown(rows: list[YouTubeDailyMetric]) -> list[dict[str, float | str]]:
    output = []
    for content_type in ["short", "live", "vod", "unknown"]:
        grouped = [row for row in rows if row.content_type == content_type]
        if not grouped:
            continue
        summary = summarize_daily_rows(grouped)
        summary["content_type"] = content_type
        summary["avg_view_duration_seconds"] = weighted_average(grouped, "avg_view_duration_seconds", "views")
        output.append(summary)
    return output


def weighted_average(rows: list[YouTubeDailyMetric], value_attr: str, weight_attr: str) -> float:
    total_weight = sum(getattr(row, weight_attr) for row in rows)
    if not total_weight:
        return 0
    return sum(getattr(row, value_attr) * getattr(row, weight_attr) for row in rows) / total_weight


def revenue_forecasts(db: Session, as_of: date | None = None) -> dict[str, float | None]:
    active_day = as_of or today_utc()
    month_start = active_day.replace(day=1)
    days_in_month = monthrange(active_day.year, active_day.month)[1]
    elapsed_days = active_day.day
    remaining_days = days_in_month - elapsed_days
    mtd_revenue = summarize_daily_rows(metrics_between(db, month_start, active_day))["estimated_revenue"]
    seven = trailing_average_revenue(db, active_day, 7)
    twenty_eight = trailing_average_revenue(db, active_day, 28)
    return {
        "mtd_run_rate": (mtd_revenue / elapsed_days * days_in_month) if elapsed_days and mtd_revenue else None,
        "seven_day_pace": (mtd_revenue + seven * remaining_days) if seven is not None else None,
        "twenty_eight_day_pace": (mtd_revenue + twenty_eight * remaining_days) if twenty_eight is not None else None,
    }


def trailing_average_revenue(db: Session, as_of: date, days: int) -> float | None:
    start = as_of - timedelta(days=days - 1)
    chart_rows = daily_chart_rows(metrics_between(db, start, as_of))
    if not chart_rows:
        return None
    return sum(float(row["revenue"]) for row in chart_rows) / days


def livestream_summaries(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    query = (
        select(YouTubeLivestreamMetric)
        .join(YouTubeVideo)
        .where(
            YouTubeVideo.content_type == "live",
            YouTubeVideo.published_at >= start.isoformat(),
            YouTubeVideo.published_at <= f"{end.isoformat()}T23:59:59",
        )
        .order_by(YouTubeVideo.published_at.desc())
    )
    return [livestream_summary(metric) for metric in db.scalars(query).all()]


def livestream_summary(metric: YouTubeLivestreamMetric) -> dict[str, Any]:
    video = metric.video
    return {
        "video_id": metric.video_id,
        "title": video.title if video else "",
        "thumbnail": video.thumbnail if video else "",
        "published_at": video.published_at if video else "",
        "duration_seconds": metric.duration_seconds or (video.duration_seconds if video else 0),
        "total_views": metric.total_views,
        "live_views": metric.live_views,
        "replay_views": metric.replay_views,
        "watch_minutes": metric.watch_minutes,
        "likes": metric.likes,
        "subscribers_gained": metric.subscribers_gained,
        "subscribers_lost": metric.subscribers_lost,
        "subscriber_net": metric.subscribers_gained - metric.subscribers_lost,
        "estimated_revenue": metric.estimated_revenue,
        "average_concurrent_viewers": metric.average_concurrent_viewers,
        "peak_concurrent_viewers": metric.peak_concurrent_viewers,
        "actual_start": metric.actual_start,
        "actual_end": metric.actual_end,
        "scheduled_start": metric.scheduled_start,
    }


def livestream_detail(db: Session, video_id: str) -> dict[str, Any] | None:
    metric = db.get(YouTubeLivestreamMetric, video_id)
    if not metric:
        return None
    timeseries = list(
        db.scalars(
            select(YouTubeLivestreamTimeseries)
            .where(YouTubeLivestreamTimeseries.video_id == video_id)
            .order_by(YouTubeLivestreamTimeseries.stream_position_seconds)
        )
    )
    stream = metric.video.stream if metric.video else None
    return {
        "summary": livestream_summary(metric),
        "stream": stream,
        "has_content_intelligence": bool(stream and (stream.transcripts or stream.analysis_artifacts or stream.candidates)),
        "timeseries": [
            {
                "stream_position_seconds": point.stream_position_seconds,
                "measured_at": point.measured_at.isoformat() if point.measured_at else "",
                "concurrent_viewers": point.concurrent_viewers,
                "average_concurrent_viewers": point.average_concurrent_viewers,
                "peak_concurrent_viewers": point.peak_concurrent_viewers,
            }
            for point in timeseries
        ],
    }


def run_youtube_analytics_sync_background(mode: str = "manual") -> None:
    with SessionLocal() as db:
        sync_youtube_analytics(db, mode=mode)
