from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", CODE_ROOT)).resolve()
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from app.services.ytdlp import (
    YtDlpError,
    download_youtube_section,
    download_youtube_source,
    fetch_youtube_metadata,
)

MAX_OWNER_DISCOVERY_VIDEOS = 200


class PrivateSourceDiscoveryUnavailable(RuntimeError):
    pass


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def choose_private_video_id(search_items: list[dict[str, Any]], detail_items: list[dict[str, Any]]) -> str:
    ordered_ids = [str(item.get("id", {}).get("videoId") or "") for item in search_items]
    details = {str(item.get("id") or ""): item for item in detail_items}
    for video_id in ordered_ids:
        if not video_id:
            continue
        status = details.get(video_id, {}).get("status", {})
        if str(status.get("privacyStatus") or "").lower() != "private":
            continue
        upload_status = str(status.get("uploadStatus") or "").lower()
        if upload_status and upload_status != "processed":
            continue
        return video_id
    return ""


def discover_private_owner_url() -> str:
    from googleapiclient.discovery import build

    previous_cwd = Path.cwd()
    try:
        os.chdir(LIVE_ROOT)
        from app.core.database import SessionLocal
        from app.services.youtube_analytics import connected_credential, credentials_from_record

        with SessionLocal() as db:
            record = connected_credential(db)
            if record is None:
                raise PrivateSourceDiscoveryUnavailable("owner_oauth_not_connected")
            channel_id = str(record.channel_id or "")
            credentials = credentials_from_record(record)
    finally:
        os.chdir(previous_cwd)

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    channel_request = (
        youtube.channels().list(part="contentDetails", maxResults=50, id=channel_id)
        if channel_id
        else youtube.channels().list(part="contentDetails", maxResults=50, mine=True)
    )
    channel_response = channel_request.execute()
    uploads_playlists = [
        str(item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads") or "")
        for item in channel_response.get("items", [])
    ]
    uploads_playlists = [playlist_id for playlist_id in uploads_playlists if playlist_id]
    if not uploads_playlists:
        raise PrivateSourceDiscoveryUnavailable("owner_uploads_playlist_unavailable")

    scanned = 0
    for uploads_playlist in uploads_playlists:
        page_token: str | None = None
        while scanned < MAX_OWNER_DISCOVERY_VIDEOS:
            remaining = MAX_OWNER_DISCOVERY_VIDEOS - scanned
            response = (
                youtube.playlistItems()
                .list(
                    part="contentDetails",
                    playlistId=uploads_playlist,
                    maxResults=min(50, remaining),
                    pageToken=page_token,
                )
                .execute()
            )
            video_ids = [
                str(item.get("contentDetails", {}).get("videoId") or "")
                for item in response.get("items", [])
            ]
            video_ids = [video_id for video_id in video_ids if video_id]
            scanned += len(video_ids)
            if video_ids:
                details_response = (
                    youtube.videos()
                    .list(part="status", id=",".join(video_ids), maxResults=len(video_ids))
                    .execute()
                )
                ordered_items = [{"id": {"videoId": video_id}} for video_id in video_ids]
                video_id = choose_private_video_id(ordered_items, list(details_response.get("items", [])))
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"
            page_token = response.get("nextPageToken")
            if not page_token or not video_ids:
                break
    raise PrivateSourceDiscoveryUnavailable(f"no_private_processed_upload_in_first_{scanned}_owner_uploads")


def resolve_probe_url() -> tuple[str, str, str]:
    configured = os.getenv("SHOCKS_PRIVATE_YOUTUBE_TEST_URL", "").strip()
    if configured:
        return configured, "configured-host-url", ""
    try:
        discovered = discover_private_owner_url()
    except PrivateSourceDiscoveryUnavailable as exc:
        return "", "unavailable", str(exc)
    except Exception as exc:
        return "", "unavailable", f"discovery_error_type={type(exc).__name__}"
    return (discovered, "owner-oauth-private-upload", "") if discovered else ("", "unavailable", "no_private_owner_upload")


def safe_ytdlp_failure(stage: str, *, source_mode: str, video_id: str = "", elapsed_seconds: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": f"Private YouTube {stage} probe failed",
        "failure_stage": stage,
        "source_mode": source_mode,
        "error_type": "YtDlpError",
        "credentials_emitted": False,
        "signed_urls_emitted": False,
    }
    if video_id:
        payload["video_id"] = video_id
    if elapsed_seconds is not None:
        payload[f"{stage}_seconds"] = round(elapsed_seconds, 3)
    return payload


def main() -> int:
    url, source_mode, discovery_status = resolve_probe_url()
    if not url:
        return emit(
            {
                "summary": "Private YouTube probe has no configured or discoverable private owner upload",
                "configured": False,
                "source_mode": source_mode,
                "discovery_status": discovery_status,
                "credentials_emitted": False,
                "signed_urls_emitted": False,
            },
            2,
        )

    metadata_started = time.monotonic()
    try:
        info = fetch_youtube_metadata(url=url, timeout=120)
    except YtDlpError:
        return emit(
            safe_ytdlp_failure(
                "metadata",
                source_mode=source_mode,
                elapsed_seconds=time.monotonic() - metadata_started,
            ),
            1,
        )
    metadata_seconds = time.monotonic() - metadata_started
    video_id = str(info.get("id") or "")

    scratch_root = Path(os.getenv("SHOCKS_HOST_SCRATCH_ROOT", tempfile.gettempdir()))
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shocks-youtube-probe-", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)

        partial_started = time.monotonic()
        try:
            partial_path = download_youtube_section(
                url=url,
                output_template=temp / "partial.%(ext)s",
                expected_path=temp / "partial.mp4",
                start_seconds=0,
                end_seconds=30,
                timeout=600,
            )
        except YtDlpError:
            return emit(
                safe_ytdlp_failure(
                    "partial",
                    source_mode=source_mode,
                    video_id=video_id,
                    elapsed_seconds=time.monotonic() - partial_started,
                ),
                1,
            )
        partial_seconds = time.monotonic() - partial_started
        partial_bytes = partial_path.stat().st_size
        if partial_bytes <= 0:
            return emit(
                {
                    **safe_ytdlp_failure("partial", source_mode=source_mode, video_id=video_id, elapsed_seconds=partial_seconds),
                    "failure_reason": "empty_output",
                },
                1,
            )

        full_started = time.monotonic()
        try:
            full_path = download_youtube_source(
                url=url,
                output_template=temp / "full.%(ext)s",
                expected_path=temp / "full.mp4",
                label="private-youtube-probe",
            )
        except YtDlpError:
            return emit(
                safe_ytdlp_failure(
                    "full",
                    source_mode=source_mode,
                    video_id=video_id,
                    elapsed_seconds=time.monotonic() - full_started,
                ),
                1,
            )
        full_seconds = time.monotonic() - full_started
        full_bytes = full_path.stat().st_size
        if full_bytes <= 0:
            return emit(
                {
                    **safe_ytdlp_failure("full", source_mode=source_mode, video_id=video_id, elapsed_seconds=full_seconds),
                    "failure_reason": "empty_output",
                },
                1,
            )

    partial_mbps = (partial_bytes * 8 / 1_000_000) / partial_seconds if partial_seconds else None
    full_mbps = (full_bytes * 8 / 1_000_000) / full_seconds if full_seconds else None
    return emit(
        {
            "summary": "Private YouTube authentication, partial retrieval, and production-path full retrieval succeeded",
            "configured": source_mode == "configured-host-url",
            "source_mode": source_mode,
            "video_id": video_id,
            "duration_seconds": info.get("duration"),
            "metadata_seconds": round(metadata_seconds, 3),
            "partial": {
                "seconds": round(partial_seconds, 3),
                "bytes": partial_bytes,
                "throughput_mbps": round(partial_mbps, 3) if partial_mbps is not None else None,
            },
            "full": {
                "seconds": round(full_seconds, 3),
                "bytes": full_bytes,
                "throughput_mbps": round(full_mbps, 3) if full_mbps is not None else None,
            },
            "production_materialization_path_proven": True,
            "authentication_policy": "shared-production-ytdlp",
            "fallback": "If bounded section retrieval is unreliable for a private source, materialize the full source through the production MediaRetriever scratch lease and delete it after use.",
            "credentials_emitted": False,
            "signed_urls_emitted": False,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
