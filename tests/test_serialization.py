from __future__ import annotations

from twitter_cli.models import MediaVariant, TweetMedia
from twitter_cli.serialization import tweet_from_dict, tweet_to_dict, tweets_from_json, tweets_to_json


def test_tweet_roundtrip_dict(tweet_factory) -> None:
    tweet = tweet_factory("42")
    payload = tweet_to_dict(tweet)
    restored = tweet_from_dict(payload)

    assert restored.id == tweet.id
    assert restored.author.screen_name == tweet.author.screen_name
    assert restored.metrics.likes == tweet.metrics.likes


def test_tweets_json_roundtrip(tweet_factory) -> None:
    tweets = [tweet_factory("1"), tweet_factory("2", lang="zh")]
    raw = tweets_to_json(tweets)
    restored = tweets_from_json(raw)

    assert [tweet.id for tweet in restored] == ["1", "2"]
    assert restored[1].lang == "zh"


def test_tweets_from_json_accepts_structured_success_envelope(tweet_factory) -> None:
    tweets = [tweet_factory("1")]
    raw = (
        "{\n"
        '  "ok": true,\n'
        '  "schema_version": "1",\n'
        '  "data": %s\n'
        "}\n"
    ) % tweets_to_json(tweets)

    restored = tweets_from_json(raw)

    assert [tweet.id for tweet in restored] == ["1"]


def test_compact_serialization(tweet_factory) -> None:
    from twitter_cli.serialization import tweet_to_compact_dict, tweets_to_compact_json
    import json

    tweet = tweet_factory(
        "42",
        created_at="Sat Mar 07 05:51:02 +0000 2026",
        text="A" * 200,
    )
    compact = tweet_to_compact_dict(tweet)

    assert compact["id"] == "42"
    assert compact["author"] == "@alice"
    assert compact["time"] == "Mar 07 05:51"
    assert len(compact["text"]) <= 140
    assert compact["text"].endswith("...")
    assert compact["likes"] == 10
    assert compact["rts"] == 2
    # Should only have 6 keys
    assert set(compact.keys()) == {"id", "author", "text", "likes", "rts", "time"}

    # Test batch serialization
    raw = tweets_to_compact_json([tweet])
    parsed = json.loads(raw)
    assert len(parsed) == 1
    assert parsed[0]["author"] == "@alice"


def test_tweet_roundtrip_preserves_article_fields(tweet_factory) -> None:
    tweet = tweet_factory(
        "88",
        article_title="Long-form title",
        article_text="Intro\n\n## Details",
    )

    payload = tweet_to_dict(tweet)
    restored = tweet_from_dict(payload)

    assert restored.article_title == "Long-form title"
    assert restored.article_text == "Intro\n\n## Details"


def test_tweet_roundtrip_preserves_subscriber_only(tweet_factory) -> None:
    tweet = tweet_factory("99", is_subscriber_only=True)
    payload = tweet_to_dict(tweet)
    assert payload["isSubscriberOnly"] is True
    restored = tweet_from_dict(payload)
    assert restored.is_subscriber_only is True


def test_tweet_roundtrip_preserves_promoted_flag(tweet_factory) -> None:
    tweet = tweet_factory("100", is_promoted=True)
    payload = tweet_to_dict(tweet)
    assert payload["isPromoted"] is True
    restored = tweet_from_dict(payload)
    assert restored.is_promoted is True


def test_tweet_roundtrip_preserves_media_preview_and_variants(tweet_factory) -> None:
    tweet = tweet_factory(
        "101",
        media=[
            TweetMedia(
                type="video",
                url="https://high.mp4",
                preview_url="https://preview.jpg",
                width=1920,
                height=1080,
                alt_text="Demo clip alt text",
                variants=[
                    MediaVariant(url="https://high.mp4", bitrate=2176000),
                    MediaVariant(url="https://low.mp4", bitrate=832000),
                ],
            )
        ],
    )

    payload = tweet_to_dict(tweet)
    assert payload["media"][0]["previewUrl"] == "https://preview.jpg"
    assert payload["media"][0]["altText"] == "Demo clip alt text"
    assert payload["media"][0]["variants"][1]["url"] == "https://low.mp4"

    restored = tweet_from_dict(payload)
    assert restored.media[0].preview_url == "https://preview.jpg"
    assert restored.media[0].alt_text == "Demo clip alt text"
    assert [variant.url for variant in restored.media[0].variants] == [
        "https://high.mp4",
        "https://low.mp4",
    ]


def test_tweet_roundtrip_preserves_reply_metadata(tweet_factory) -> None:
    tweet = tweet_factory(
        "102",
        conversation_id="100",
        in_reply_to_status_id="100",
        in_reply_to_screen_name="dashhuang",
    )

    payload = tweet_to_dict(tweet)
    assert payload["conversationId"] == "100"
    assert payload["inReplyToStatusId"] == "100"
    assert payload["inReplyToScreenName"] == "dashhuang"

    restored = tweet_from_dict(payload)
    assert restored.conversation_id == "100"
    assert restored.in_reply_to_status_id == "100"
    assert restored.in_reply_to_screen_name == "dashhuang"
