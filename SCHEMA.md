# Structured Output Schema

`twitter-cli` uses a shared agent-friendly envelope for machine-readable output.

## Success

```yaml
ok: true
schema_version: "1"
data: ...
pagination:
  nextCursor: "optional-cursor"
```

## Error

```yaml
ok: false
schema_version: "1"
error:
  code: api_error
  message: User @foo not found
```

## Notes

- `--yaml` and `--json` both use this envelope
- non-TTY stdout defaults to YAML
- tweet and user lists are returned under `data`
- `lists` returns owned and followed list metadata under `data`
- timeline-style list commands may also return `pagination.nextCursor`
- `article` returns a single tweet object under `data`
- `status` returns `data.authenticated` plus `data.user`
- `whoami` returns `data.user`
- `user-posts --cursor ...` returns tweets under `data` and may include `pagination.nextCursor`
- `notifications` returns account activity under `data` and may include `pagination.nextCursor`
- write commands also support explicit `--json` / `--yaml`

## Notification Fields

```yaml
data:
  - id: "notification-id"
    type: like
    message: Alice liked your post
    timestampMs: 1700000000000
    tweetId: "1234567890"
```

Known `type` values are `like`, `follow`, `retweet`, `mention`, `reply`,
`quote`, and `unknown`.

## Translation Fields

`twitter translate <id> --to <language> --json` returns:

```yaml
data:
  id: "1234567890"
  translation: "Translated tweet text"
  sourceLanguage: ja
  localizedSourceLanguage: Japanese
  destinationLanguage: en
  translationSource: Google
  translationState: Success
```

## Article Fields

`twitter article <id> --json` returns the standard tweet object plus:

```yaml
data:
  id: "1234567890"
  articleTitle: "Article Title"
  articleText: |
    # Heading
    Body text...
```

## Reply Fields

Tweet objects may also include lightweight reply metadata:

```yaml
data:
  id: "2039624925976392014"
  conversationId: "2039306742803370051"
  inReplyToStatusId: "2039306742803370051"
  inReplyToScreenName: "DashHuang"
```

## User Fields

User objects may also include lightweight viewer relationship state:

```yaml
data:
  id: "42"
  screenName: "alice"
  viewerFollowing: false
  viewerFollowedBy: true
  viewerBlocking: false
  viewerMuting: false
```

## Media Fields

Video-like `media` items may also include:

```yaml
media:
  - type: video
    url: "highest-quality-mp4"
    previewUrl: "https://pbs.twimg.com/ext_tw_video_thumb/..."
    variants:
      - url: "https://video.twimg.com/...mp4"
        bitrate: 2176000
      - url: "https://video.twimg.com/...mp4"
        bitrate: 832000
```

## Error Codes

Common structured error codes:

- `not_authenticated`
- `not_found`
- `invalid_input`
- `rate_limited`
- `api_error`
