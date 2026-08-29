from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = Path(os.getenv("SHOCKS_HOST_LIVE_ROOT", CODE_ROOT)).resolve()
MAX_OWNER_DISCOVERY_VIDEOS = 200


class PrivateSourceDiscoveryUnavailable(RuntimeError):
    pass


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return code


def choose_private_video_id(ordered_ids: list[str], detail_items: list[dict[str, Any]]) -> str:
    details = {str(item.get("id") or ""): item for item in detail_items}
    for video_id in ordered_ids:
        status = details.get(video_id, {}).get("status", {})
        if str(status.get("privacyStatus") or "").lower() != "private":
            continue
        upload_status = str(status.get("uploadStatus") or "").lower()
        if upload_status and upload_status != "processed":
            continue
        return video_id
    return ""


def configure_live_imports() -> None:
    """Make the live production checkout authoritative for app imports.

    This helper always runs in a fresh interpreter so candidate-worktree app
    modules can never be cached before production OAuth/database imports.
    """

    live = str(LIVE_ROOT)
    candidate = str(CODE_ROOT)
    filtered: list[str] = []
    for entry in sys.path:
        try:
            resolved = str(Path(entry or ".").resolve())
        except OSError:
            resolved = entry
        if resolved == candidate or resolved.startswith(candidate + os.sep):
            continue
        if resolved == live:
            continue
        filtered.append(entry)
    sys.path[:] = [live, *filtered]
    os.chdir(LIVE_ROOT)


def discover_private_owner_video_id() -> str:
    configure_live_imports()

    from googleapiclient.discovery import build

    from app.core.database import SessionLocal
    from app.services.youtube_analytics import connected_credential, credentials_from_record

    with SessionLocal() as db:
        record = connected_credential(db)
        if record is None:
            raise PrivateSourceDiscoveryUnavailable("owner_oauth_not_connected")
        channel_id = str(record.channel_id or "")
        credentials = credentials_from_record(record)

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
                detail_response = (
                    youtube.videos()
                    .list(part="status", id=",".join(video_ids), maxResults=len(video_ids))
                    .execute()
                )
                private_id = choose_private_video_id(video_ids, list(detail_response.get("items", [])))
                if private_id:
                    return private_id
            page_token = response.get("nextPageToken")
            if not page_token or not video_ids:
                break

    raise PrivateSourceDiscoveryUnavailable(f"no_private_processed_upload_in_first_{scanned}_owner_uploads")


def main() -> int:
    try:
        video_id = discover_private_owner_video_id()
    except PrivateSourceDiscoveryUnavailable as exc:
        return emit({"ok": False, "status": str(exc)}, 2)
    except Exception as exc:
        return emit({"ok": False, "status": f"discovery_error_type={type(exc).__name__}"}, 1)
    return emit({"ok": True, "video_id": video_id})


if __name__ == "__main__":
    raise SystemExit(main())
