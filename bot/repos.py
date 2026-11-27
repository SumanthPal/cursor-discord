"""Repo names, and the channel ↔ repo binding stored in the channel topic (`cursor-repo:…`)."""
from __future__ import annotations

import logging
import re

import discord

logger = logging.getLogger(__name__)

CHANNEL_REPO_RE = re.compile(r"(?:^|\s)cursor-repo:(\S+)(?:\s|$)", re.IGNORECASE)
TOPIC_MAX = 1024

_GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?(?=[\s,.;:!?)]|$)",
    re.IGNORECASE,
)
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKEN_TRIM = "`'\"()[]<>,.;:!?"


def normalize_repo(value: str) -> str:
    value = value.strip().strip("`")
    if value.startswith(("http://", "https://")):
        value = value.rstrip("/")
        return value[:-4] if value.endswith(".git") else value
    if value.count("/") == 1 and " " not in value:
        return f"https://github.com/{value}"
    return value.rstrip("/")


def compact_repo(repo: str) -> str:
    url = normalize_repo(repo)
    for prefix in ("https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            return url.removeprefix(prefix)
    return url


def same_repo(a: str, b: str) -> bool:
    return normalize_repo(a).lower() == normalize_repo(b).lower()


def repo_tokens(text: str) -> list[str]:
    """Every GitHub URL or `owner/repo`-shaped word in ``text``, as clone URLs."""
    out: list[str] = []
    for word in (text or "").split():
        url = _GITHUB_URL_RE.match(word)
        word = word.strip(_TOKEN_TRIM)
        if url:
            repo = normalize_repo(url.group(1))
        elif _OWNER_REPO_RE.match(word) and "." not in word.split("/")[0]:
            repo = normalize_repo(word)
        else:
            continue
        if not any(same_repo(repo, seen) for seen in out):
            out.append(repo)
    return out


def find_repo_in_text(text: str, candidates: list[str] | None = None) -> str | None:
    """The repo a message names, if any.

    A GitHub URL anywhere counts. A bare ``owner/repo`` counts when it is the first
    word, or anywhere if it matches one of ``candidates`` (so a path like
    ``src/app.py`` mid-sentence isn't mistaken for a repo).
    """
    raw = (text or "").strip()
    url = _GITHUB_URL_RE.search(raw)
    if url:
        return normalize_repo(url.group(1))
    words = [w.strip(_TOKEN_TRIM) for w in raw.split()]
    if words and _OWNER_REPO_RE.match(words[0]):
        return normalize_repo(words[0])
    for word in words:
        if not _OWNER_REPO_RE.match(word):
            continue
        for cand in candidates or []:
            if same_repo(word, cand):
                return normalize_repo(cand)
    return None


def match_allowed_repo(typed: str, allowed: list[str]) -> str | None:
    """Resolve a typed URL / owner/repo / name fragment to one URL in ``allowed``."""
    typed = (typed or "").strip().strip(_TOKEN_TRIM)
    if not typed or not allowed:
        return None
    want = normalize_repo(typed)
    for url in allowed:
        if same_repo(url, want):
            return url.rstrip("/")
    needle = compact_repo(want).lower()
    hits = [u for u in allowed if needle in compact_repo(u).lower()]
    if len(hits) == 1:
        return hits[0].rstrip("/")
    return None


def parse_channel_repos(topic: str | None) -> list[str]:
    match = CHANNEL_REPO_RE.search(topic or "")
    if not match:
        return []
    out: list[str] = []
    for part in match.group(1).split(","):
        if part.strip():
            url = normalize_repo(part)
            if url not in out:
                out.append(url)
    return out


def set_channel_repo_topic(topic: str | None, repos: list[str]) -> str:
    """Upsert (or, for an empty list, remove) `cursor-repo:owner/a,owner/b` in a topic."""
    base = topic or ""
    if not repos:
        return " ".join(CHANNEL_REPO_RE.sub(" ", base).split())
    marker = "cursor-repo:" + ",".join(compact_repo(r) for r in repos)
    if CHANNEL_REPO_RE.search(base):
        return CHANNEL_REPO_RE.sub(f" {marker} ", base).strip()
    return f"{base.rstrip()} {marker}".strip()


def format_repos(repos: list[str]) -> str:
    """`a`, `b` and `c`."""
    labels = [f"`{compact_repo(r)}`" for r in repos]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


async def topic_channel(
    bot: discord.Client, channel: discord.abc.Messageable | None
) -> discord.TextChannel | discord.ForumChannel | None:
    """The text/forum channel whose topic holds the binding (threads → parent)."""
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return channel
    if not isinstance(channel, discord.Thread) or channel.parent_id is None:
        return None
    parent = channel.parent or bot.get_channel(channel.parent_id)
    if parent is None:
        try:
            parent = await bot.fetch_channel(channel.parent_id)
        except discord.HTTPException:
            logger.warning("could not fetch parent channel %s", channel.parent_id)
            return None
    if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return parent
    return None


async def channel_repos(
    bot: discord.Client, channel: discord.abc.Messageable | None
) -> list[str]:
    target = await topic_channel(bot, channel)
    return parse_channel_repos(target.topic if target else None)


async def save_channel_repos(
    bot: discord.Client, channel: discord.abc.Messageable | None, repos: list[str]
) -> str | None:
    """Write the binding to the channel topic. Returns a problem to tell the user, or None."""
    target = await topic_channel(bot, channel)
    if target is None:
        return "I can only remember repos for regular text or forum channels."
    new_topic = set_channel_repo_topic(target.topic, repos)
    if len(new_topic) > TOPIC_MAX:
        return "That's more repos than fit in a channel topic. Try fewer."
    me = target.guild.me
    if me is None or not target.permissions_for(me).manage_channels:
        return (
            f"I need the **Manage Channels** permission in #{target.name} to remember "
            f"that. You can also set the channel topic to `{new_topic}` yourself."
        )
    try:
        await target.edit(topic=new_topic)
    except discord.HTTPException:
        logger.warning("topic edit failed for #%s", target.name, exc_info=True)
        return f"Discord wouldn't let me update the topic. Set it to `{new_topic}` yourself?"
    logger.info("channel #%s (%s) repos → %s", target.name, target.id, repos)
    return None
