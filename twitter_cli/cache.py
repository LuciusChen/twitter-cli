"""Short-index cache: persist the last displayed tweet list for quick `show` access."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .models import Tweet

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".twitter-cli"
_CACHE_FILE = _CACHE_DIR / "last_results.json"
_TTL = 3600  # seconds


def _secure_cache_directory(directory: Path) -> None:
    if os.name == "posix":
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise OSError(f"Expected a cache directory at {directory}")
        if stat.S_IMODE(mode) != 0o700:
            directory.chmod(0o700)


def read_cache_json(path: Path) -> Any:
    """Read cached JSON, tightening permissions left by older releases."""
    _secure_cache_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, encoding="utf-8") as stream:
        if os.name == "posix" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return json.load(stream)


def write_cache_json(path: Path, payload: dict) -> None:
    """Atomically replace cached JSON with a private file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_cache_directory(path.parent)
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False,
    )
    try:
        with temporary as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temporary.name, path)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def save_tweet_cache(tweets: List[Tweet]) -> None:
    """Persist tweet list so indices can be resolved by `show`."""
    try:
        entries = [
            {"index": i + 1, "id": t.id, "author": t.author.screen_name, "text": t.text[:80]}
            for i, t in enumerate(tweets)
            if t.id
        ]
        payload = {"created_at": time.time(), "tweets": entries}
        write_cache_json(_CACHE_FILE, payload)
    except OSError as exc:
        logger.debug("Failed to write tweet cache: %s", exc)


def _load_cache() -> Optional[List[dict]]:
    """Load and validate the cache file; return tweet entries or None if stale/missing."""
    try:
        payload = read_cache_json(_CACHE_FILE)
        if not isinstance(payload, dict):
            return None
        if time.time() - payload.get("created_at", 0) > _TTL:
            return None
        entries = payload.get("tweets", [])
        if not isinstance(entries, list):
            return None
        return [e for e in entries if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return None


def resolve_cached_tweet(index: int) -> Tuple[Optional[str], int]:
    """Resolve a 1-based index to a tweet ID, returning (tweet_id, cache_size).

    Returns (tweet_id, cache_size) where tweet_id is None if the index
    cannot be resolved (empty/expired cache or out-of-range index).
    """
    entries = _load_cache()
    if entries is None:
        return None, 0
    for entry in entries:
        if entry.get("index") == index:
            tweet_id = entry.get("id")
            return (str(tweet_id) if tweet_id is not None else None), len(entries)
    return None, len(entries)