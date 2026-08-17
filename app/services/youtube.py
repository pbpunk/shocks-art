import re


ISO_DURATION_RE = re.compile(r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?")


def parse_iso8601_duration(value: str) -> int:
    match = ISO_DURATION_RE.fullmatch(value or "")
    if not match:
        return 0
    return int(match.group("h") or 0) * 3600 + int(match.group("m") or 0) * 60 + int(match.group("s") or 0)


class YouTubeClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def discover_streams(self, channel_handle: str) -> list[dict]:
        if not self.api_key:
            raise RuntimeError("YOUTUBE_API_KEY is required for live discovery")

        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", developerKey=self.api_key)
        channel = (
            youtube.channels()
            .list(part="id,snippet", forHandle=channel_handle if channel_handle.startswith("@") else f"@{channel_handle}")
            .execute()
        )
        items = channel.get("items", [])
        if not items:
            raise RuntimeError(f"No YouTube channel found for handle {channel_handle}")
        channel_id = items[0]["id"]
        return self._discover_for_channel_id(youtube, channel_id)

    def _discover_for_channel_id(self, youtube, channel_id: str) -> list[dict]:
        videos: list[dict] = []
        page_token: str | None = None
        while True:
            response = (
                youtube.search()
                .list(
                    part="id,snippet",
                    channelId=channel_id,
                    eventType="completed",
                    type="video",
                    order="date",
                    maxResults=50,
                    pageToken=page_token,
                )
                .execute()
            )
            ids = [item["id"]["videoId"] for item in response.get("items", [])]
            if ids:
                videos.extend(self._fetch_video_details(youtube, channel_id, ids))
            page_token = response.get("nextPageToken")
            if not page_token:
                return videos

    def _fetch_video_details(self, youtube, channel_id: str, video_ids: list[str]) -> list[dict]:
        response = youtube.videos().list(part="snippet,contentDetails", id=",".join(video_ids), maxResults=50).execute()
        streams: list[dict] = []
        for item in response.get("items", []):
            snippet = item["snippet"]
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
            video_id = item["id"]
            streams.append(
                {
                    "platform": "youtube",
                    "channel_id": channel_id,
                    "source_video_id": video_id,
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "published_at": snippet.get("publishedAt", ""),
                    "duration": parse_iso8601_duration(item.get("contentDetails", {}).get("duration", "")),
                    "thumbnail": thumbnail.get("url", ""),
                    "processing_status": "queued",
                    "schema_version": "1.0",
                }
            )
        return streams

