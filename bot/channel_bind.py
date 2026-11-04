"""Channel ↔ repo binding via Discord channel topic (`cursor-repo:…`)."""

from __future__ import annotations

import logging
from typing import Union

import discord

from bot.projects import parse_channel_repo, set_channel_repo_topic

logger = logging.getLogger(__name__)

TopicChannel = Union[discord.TextChannel, discord.ForumChannel]


async def resolve_topic_channel(
    bot: discord.Client,
    channel: discord.abc.Messageable | None,
) -> TopicChannel | None:
    """Text/Forum channel whose topic we read/write. Threads → parent.

    Slash interactions sometimes give ``PartialMessageable`` (no ``.topic``);
    resolve via cache / fetch so pin + lookup actually work.
    """
    if channel is None:
        return None

    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            return parent
        if channel.parent_id is None:
            return None
        return await _fetch_topic_channel(bot, channel.parent_id)

    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return channel

    cid = getattr(channel, "id", None)
    if not isinstance(cid, int):
        return None
    return await _fetch_topic_channel(bot, cid)


async def _fetch_topic_channel(
    bot: discord.Client, channel_id: int
) -> TopicChannel | None:
    found = bot.get_channel(channel_id)
    if found is None:
        try:
            found = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.warning(
                "fetch_channel(%s) failed for topic bind", channel_id, exc_info=True
            )
            return None
    if isinstance(found, discord.Thread):
        return await resolve_topic_channel(bot, found)
    if isinstance(found, (discord.TextChannel, discord.ForumChannel)):
        return found
    return None


async def pin_channel_repo(
    bot: discord.Client,
    channel: discord.abc.Messageable | None,
    repo_url: str,
    *,
    force: bool = False,
) -> str:
    """Write ``cursor-repo:…`` into the channel topic. Returns a short user-facing note."""
    target = await resolve_topic_channel(bot, channel)
    if target is None:
        logger.warning("channel repo pin skipped: could not resolve topic channel")
        return (
            "Couldn't pin a channel default (not a text/forum channel I can see). "
            "You'll keep getting the repo picker here."
        )

    existing = parse_channel_repo(target.topic)
    if existing and not force:
        return f"Channel already pinned to `{_compact(existing)}`."

    me = target.guild.me
    if me is None or not target.permissions_for(me).manage_channels:
        logger.warning(
            "channel repo pin skipped: missing Manage Channels in #%s (%s)",
            target.name,
            target.id,
        )
        return (
            f"Couldn't pin **#{target.name}** — I need **Manage Channels** "
            f"here (server invite isn't enough if this channel denies it). "
            f"Fix perms or set topic to `cursor-repo:{_compact(repo_url)}` manually."
        )

    new_topic = set_channel_repo_topic(target.topic, repo_url)
    try:
        await target.edit(topic=new_topic)
    except discord.HTTPException:
        logger.warning(
            "channel repo pin failed for #%s (%s)", target.name, target.id, exc_info=True
        )
        return (
            f"Couldn't update topic on **#{target.name}** (Discord rejected the edit). "
            f"Set `cursor-repo:{_compact(repo_url)}` manually?"
        )

    logger.info(
        "pinned channel #%s (%s) → %s", target.name, target.id, _compact(repo_url)
    )
    return f"Pinned **#{target.name}** → `{_compact(repo_url)}` (saved in channel topic)."


def topic_from_channel(channel: TopicChannel | None) -> str | None:
    if channel is None:
        return None
    return channel.topic


def _compact(repo: str) -> str:
    url = repo.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            return url.removeprefix(prefix)
    return url
