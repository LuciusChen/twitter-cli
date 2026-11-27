# Changelog

This file records user-visible changes maintained on the `stable` branch from
2026-08-01 onward. Earlier history remains available in Git.

## Unreleased

### Added

- `twitter translate <ID-or-URL> --to <language>` for on-demand tweet
  translation through X's authenticated web endpoint.
- `twitter notifications` for account activity including mentions, replies,
  quotes, likes, follows, and reposts.
- `twitter users <query>` for user typeahead search.
- Bookmark timelines, including individual folders, accept `--cursor` and
  expose the continuation in structured output.
- Structured tweets and media include X's sensitive-content labels.
- Thread detail supports `--cursor`, and structured tweets include the replied-to
  user ID (`inReplyToUserId`) when X provides it.

### Fixed

- Restrict local cache files and their directory to the current user on POSIX.

- Resume a timeline from the last returned tweet when X sends an oversized page.
- Require curl_cffi 0.15.0 or later for GHSA-qw2m-4pqf-rmpp.
