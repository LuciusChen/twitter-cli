from __future__ import annotations

import json
from io import StringIO

from twitter_cli.daemon import TwitterDaemon
from twitter_cli.models import UserProfile


def test_daemon_stdio_emits_ready_and_feed_payload(monkeypatch, tweet_factory) -> None:
    closed = False

    class FakeClient:
        def fetch_home_timeline(self, count: int, cursor=None, return_cursor=False):
            assert count == 20
            assert cursor == "cursor-prev"
            assert return_cursor is True
            return [tweet_factory("1")], "cursor-next"

    daemon = TwitterDaemon(config={"fetch": {"count": 50}, "rateLimit": {}})
    daemon._client = FakeClient()

    def fake_close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr("twitter_cli.daemon.close_shared_session", fake_close)

    input_stream = StringIO(
        json.dumps({"id": "1", "args": ["feed", "--cursor", "cursor-prev", "--max", "20"]})
        + "\n"
        + json.dumps({"id": "2", "args": ["shutdown"]})
        + "\n"
    )
    output_stream = StringIO()

    assert daemon.run_stdio(input_stream, output_stream) == 0

    lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert lines[0]["event"] == "ready"
    assert lines[1]["id"] == "1"
    assert lines[1]["ok"] is True
    assert lines[1]["pagination"]["nextCursor"] == "cursor-next"
    assert lines[1]["data"][0]["id"] == "1"
    assert lines[2]["id"] == "2"
    assert lines[2]["data"]["shutdown"] is True
    assert closed is True


def test_daemon_user_posts_reuses_client(tweet_factory) -> None:
    calls: list[str] = []
    profile = UserProfile(id="u1", name="Alice", screen_name="alice")

    class FakeClient:
        def fetch_user(self, screen_name: str) -> UserProfile:
            calls.append(f"user:{screen_name}")
            return profile

        def fetch_user_tweets(self, user_id: str, count: int):
            calls.append(f"tweets:{user_id}:{count}")
            return [tweet_factory("9")]

    daemon = TwitterDaemon(config={"fetch": {"count": 50}, "rateLimit": {}})
    daemon._client = FakeClient()
    input_stream = StringIO(
        json.dumps({"id": "1", "args": ["user-posts", "alice", "--max", "15"]})
        + "\n"
        + json.dumps({"id": "2", "args": ["shutdown"]})
        + "\n"
    )
    output_stream = StringIO()

    daemon.run_stdio(input_stream, output_stream)

    lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert calls == ["user:alice", "tweets:u1:15"]
    assert lines[1]["data"][0]["id"] == "9"


def test_daemon_returns_structured_error_for_unsupported_option() -> None:
    daemon = TwitterDaemon(config={"fetch": {"count": 50}, "rateLimit": {}})
    daemon._client = object()
    input_stream = StringIO(
        json.dumps({"id": "1", "args": ["feed", "--bogus"]})
        + "\n"
        + json.dumps({"id": "2", "args": ["shutdown"]})
        + "\n"
    )
    output_stream = StringIO()

    daemon.run_stdio(input_stream, output_stream)

    lines = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert lines[1]["id"] == "1"
    assert lines[1]["ok"] is False
    assert "Unsupported daemon option" in lines[1]["error"]["message"]
