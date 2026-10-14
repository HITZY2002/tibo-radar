"""Read Tibo signals from the public codex-reset.com feed."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CodexResetFeedError(Exception):
    """Raised when the public feed is unavailable, stale, or malformed."""


class CodexResetFetcher:
    """Fetch normalized Tibo posts from the public Codex Reset JSON API."""

    def __init__(
        self,
        feed_url: str = "https://codex-reset.com/api/feed",
        timeout: int = 20,
        max_age_hours: float = 6,
    ):
        self.feed_url = feed_url.strip()
        self.timeout = timeout
        self.max_age_hours = max_age_hours
        self.last_report: dict[str, Any] = {}

    async def fetch_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch and normalize recent posts, newest first."""
        try:
            payload = await asyncio.to_thread(self._request_json)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise CodexResetFeedError(f"Codex Reset API request failed: {exc}") from exc

        tweets = payload.get("tweets")
        if not isinstance(tweets, list):
            raise CodexResetFeedError("Codex Reset API response has no tweets array")

        feed_age_hours = self._age_from_timestamp(payload.get("fetched_at"))
        content_age_days = payload.get("content_age_days")
        try:
            content_age_hours = float(content_age_days) * 24
        except (TypeError, ValueError):
            content_age_hours = None

        # content_age_days is the age of the newest Tibo post, not the feed fetch.
        # A quiet account must not make a freshly fetched feed look stale.
        if payload.get("stale") or feed_age_hours > self.max_age_hours:
            raise CodexResetFeedError(
                f"Codex Reset API is stale ({feed_age_hours:.1f} hours old)"
            )

        events_by_id = {
            str(item.get("id")): item
            for item in payload.get("events", [])
            if isinstance(item, dict) and str(item.get("id", "")).isdigit()
        }
        posts: list[dict[str, Any]] = []
        for tweet in tweets:
            post_id = str(tweet.get("id", ""))
            text = tweet.get("text")
            if not post_id.isdigit() or not isinstance(text, str) or not text.strip():
                continue
            source_event = events_by_id.get(post_id, {})
            posts.append(
                {
                    "id": post_id,
                    "text": text.strip(),
                    "timestamp": tweet.get("at") or tweet.get("declared_at"),
                    "url": tweet.get("url")
                    or f"https://x.com/thsottiaux/status/{post_id}",
                    "isReply": bool(tweet.get("is_reply")),
                    "isQuote": False,
                    "isRetweet": False,
                    "content_complete": True,
                    "source": "codex-reset",
                    "source_kind": tweet.get("kind"),
                    "source_lane": tweet.get("tibo_lane"),
                    "source_event_type": source_event.get("type"),
                    "source_announcement_state": source_event.get("announcement_state"),
                    "source_confidence": source_event.get("confidence"),
                }
            )

        posts.sort(key=lambda post: int(post["id"]), reverse=True)
        posts = posts[: max(1, limit)]
        self.last_report = {
            "source": "codex-reset",
            "fetched_at": payload.get("fetched_at"),
            "feed_age_hours": round(feed_age_hours, 2),
            "content_age_hours": (
                round(content_age_hours, 2) if content_age_hours is not None else None
            ),
            "posts": len(posts),
            "source_scope": payload.get("source_scope"),
            "stale": bool(payload.get("stale")),
        }
        return posts

    def _request_json(self) -> dict[str, Any]:
        request = Request(
            self.feed_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AstrBot-Tibo-Monitor/0.3",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise CodexResetFeedError("Codex Reset API response is not an object")
        return data

    @staticmethod
    def _age_from_timestamp(value: Any) -> float:
        if not value:
            return float("inf")
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return max(
                0.0,
                (
                    datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
                ).total_seconds()
                / 3600,
            )
        except (TypeError, ValueError):
            return float("inf")
