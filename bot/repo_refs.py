from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from bot.projects import normalize_repo

# Discord SelectOption.value / app_commands.Choice.value max length.
DISCORD_VALUE_MAX = 100


def repo_path(url: str) -> str:
    """owner/repo (or path) form — usually fits Discord's 100-char value limit."""
    path = urlparse(url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path or url


def encode_repo_ref(url: str, cache: dict[str, str]) -> str:
    """
    Encode a full repo URL into a Discord-safe ≤100 char value.

    Prefer compact path (owner/repo). If that (or the URL) exceeds the limit,
    store the full URL under a short hash key in `cache`.
    """
    url = url.rstrip("/")
    path = repo_path(url)
    if len(path) <= DISCORD_VALUE_MAX:
        cache[path] = url
        return path
    if len(url) <= DISCORD_VALUE_MAX:
        cache[url] = url
        return url
    key = "h:" + hashlib.sha1(url.encode()).hexdigest()[:20]
    cache[key] = url
    return key


def repos_matching(urls: list[str], query: str) -> list[str]:
    """Case-insensitive substring match on owner/repo path or full URL."""
    needle = (query or "").strip().lower()
    if not needle:
        return list(urls)
    out: list[str] = []
    for url in urls:
        if needle in url.lower() or needle in repo_path(url).lower():
            out.append(url)
    return out


def decode_repo_ref(value: str, cache: dict[str, str]) -> str:
    """Resolve a Discord choice/select value back to a full clone URL."""
    value = (value or "").strip()
    if not value:
        raise ValueError("empty repo ref")
    if value in cache:
        return cache[value]
    return normalize_repo(value)
