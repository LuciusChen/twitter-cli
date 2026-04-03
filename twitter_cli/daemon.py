"""Persistent stdio daemon for twitter-cli read commands."""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.parse
from typing import Any, TextIO

from .auth import get_cookies
from .client import TwitterClient, close_shared_session
from .config import load_config
from .exceptions import TwitterError
from .output import error_payload, success_payload
from .serialization import tweet_to_dict, tweets_to_data, user_profile_to_dict

logger = logging.getLogger(__name__)


def _normalize_tweet_target(value: str) -> str:
    """Extract a numeric tweet id from raw input or a full x.com URL."""
    raw = value.strip()
    if not raw:
        raise RuntimeError("Tweet ID or URL is required")

    parsed = urllib.parse.urlparse(raw)
    candidate = raw
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        match = re.search(r"/(?:status|article)/(\d+)$", path)
        if not match:
            raise RuntimeError(f"Invalid tweet URL: {value}")
        candidate = match.group(1)
    else:
        candidate = raw.rstrip("/").split("/")[-1]
        candidate = candidate.split("?", 1)[0].split("#", 1)[0]

    if not candidate.isdigit():
        raise RuntimeError(f"Invalid tweet ID: {value}")
    return candidate


def _normalize_screen_name(value: str) -> str:
    """Strip a leading @ from screen names."""
    return value.lstrip("@")


class TwitterDaemon:
    """Serve repeated read requests over stdio with one shared client."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or load_config()
        self._client: TwitterClient | None = None

    def close(self) -> None:
        """Release shared network resources."""
        self._client = None
        close_shared_session()

    def warm(self) -> None:
        """Initialize the shared client eagerly."""
        self._get_client()

    def _get_client(self) -> TwitterClient:
        if self._client is None:
            cookies = get_cookies()
            rate_limit_config = self._config.get("rateLimit")
            self._client = TwitterClient(
                cookies["auth_token"],
                cookies["ct0"],
                rate_limit_config,
                cookie_string=cookies.get("cookie_string"),
            )
        return self._client

    def _configured_count(self, max_count: int | None) -> int:
        configured = int(self._config.get("fetch", {}).get("count", 50))
        if max_count is None:
            return max(configured, 1)
        if max_count <= 0:
            raise RuntimeError("--max must be greater than 0")
        return max_count

    def _parse_args(self, args: list[str]) -> tuple[str, dict[str, Any]]:
        if not args:
            raise RuntimeError("Daemon request args are required")

        command = args[0]
        rest = list(args[1:])
        params: dict[str, Any] = {}
        index = 0

        def take_value(option: str) -> str:
            nonlocal index
            if index + 1 >= len(rest):
                raise RuntimeError(f"Missing value for {option}")
            value = rest[index + 1]
            index += 2
            return value

        if command in {"tweet", "article", "user", "user-posts", "search"}:
            if not rest:
                raise RuntimeError(f"{command} requires a target argument")
            params["target"] = rest[0]
            index = 1

        if command in {"tweet", "article"}:
            params["target"] = _normalize_tweet_target(params["target"])
        elif command in {"user", "user-posts"}:
            params["target"] = _normalize_screen_name(params["target"])

        while index < len(rest):
            token = rest[index]
            if token in {"--max", "-n"}:
                params["max_count"] = int(take_value(token))
            elif token in {"--cursor"}:
                params["cursor"] = take_value(token)
            elif token in {"--type", "-t"}:
                params["feed_type"] = take_value(token)
            else:
                raise RuntimeError(f"Unsupported daemon option: {token}")

        return command, params

    def _dispatch(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        client = self._get_client()

        if command == "feed":
            feed_type = params.get("feed_type", "for-you")
            count = self._configured_count(params.get("max_count"))
            cursor = params.get("cursor")
            if feed_type == "following":
                tweets, next_cursor = client.fetch_following_feed(
                    count, cursor=cursor, return_cursor=True
                )
            else:
                tweets, next_cursor = client.fetch_home_timeline(
                    count, cursor=cursor, return_cursor=True
                )
            payload = success_payload(tweets_to_data(tweets))
            if next_cursor:
                payload["pagination"] = {"nextCursor": next_cursor}
            return payload

        if command == "bookmarks":
            count = self._configured_count(params.get("max_count"))
            return success_payload(tweets_to_data(client.fetch_bookmarks(count)))

        if command == "search":
            count = self._configured_count(params.get("max_count"))
            return success_payload(
                tweets_to_data(client.fetch_search(params["target"], count, "Top"))
            )

        if command == "tweet":
            count = self._configured_count(params.get("max_count"))
            return success_payload(
                tweets_to_data(client.fetch_tweet_detail(params["target"], count))
            )

        if command == "article":
            return success_payload(tweet_to_dict(client.fetch_article(params["target"])))

        if command == "user":
            return success_payload(user_profile_to_dict(client.fetch_user(params["target"])))

        if command == "user-posts":
            count = self._configured_count(params.get("max_count"))
            profile = client.fetch_user(params["target"])
            return success_payload(
                tweets_to_data(client.fetch_user_tweets(profile.id, count))
            )

        if command == "shutdown":
            return success_payload({"shutdown": True})

        raise RuntimeError(f"Unsupported daemon command: {command}")

    def _handle_request(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        request_id = request.get("id")
        if request_id is None:
            raise RuntimeError("Daemon request must include an id")

        args = request.get("args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise RuntimeError("Daemon request args must be a string list")

        command, params = self._parse_args(args)
        payload = self._dispatch(command, params)
        payload["id"] = request_id
        return payload, command == "shutdown"

    @staticmethod
    def _emit_line(payload: dict[str, Any], stream: TextIO) -> None:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()

    def run_stdio(
        self,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> int:
        """Serve newline-delimited JSON requests on stdin/stdout."""
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout

        self.warm()
        self._emit_line({"event": "ready", "ok": True, "schema_version": "1"}, output_stream)

        try:
            for raw_line in input_stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise RuntimeError("Daemon request must be a JSON object")
                    payload, should_stop = self._handle_request(request)
                except (TwitterError, RuntimeError, ValueError) as exc:
                    request_id = None
                    if isinstance(request, dict):
                        request_id = request.get("id")
                    payload = error_payload(
                        getattr(exc, "error_code", "api_error"),
                        str(exc),
                    )
                    if request_id is not None:
                        payload["id"] = request_id
                    should_stop = False
                self._emit_line(payload, output_stream)
                if should_stop:
                    return 0
        finally:
            self.close()

        return 0
