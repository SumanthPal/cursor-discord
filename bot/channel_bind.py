"""Channel ↔ repo binding via Discord channel topic (`cursor-repo:…`)."""

from __future__ import annotations

import logging
from typing import Union

import discord

from bot.projects import (
    clear_channel_repo_topic,
    compact_repo,
    parse_channel_repos,
    set_channel_repo_topic,
)

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


TOPIC_MAX = 1024


async def pin_channel_repo(
    bot: discord.Client,
    channel: discord.abc.Messageable | None,
    repo_urls: list[str],
    *,
    force: bool = False,
) -> str:
    """Write ``cursor-repo:a,b`` into the channel topic (empty list clears it).

    Returns a short user-facing note.
    """
    target = await resolve_topic_channel(bot, channel)
    if target is None:
        logger.warning("channel repo pin skipped: could not resolve topic channel")
        return (
            "Couldn't pin a channel default (not a text/forum channel I can see). "
            "You'll keep getting asked which repo to use here."
        )

    existing = parse_channel_repos(target.topic)
    if existing and not force:
        return f"Channel already pinned to {_fmt(existing)}."

    if repo_urls:
        new_topic = set_channel_repo_topic(target.topic, repo_urls)
    else:
        new_topic = clear_channel_repo_topic(target.topic)
    if len(new_topic) > TOPIC_MAX:
        return (
            f"Too many repos for one channel topic ({len(new_topic)}/{TOPIC_MAX} "
            "chars). Pick fewer."
        )

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
            f"Fix perms or set the topic to `{new_topic}` manually."
        )

    try:
        await target.edit(topic=new_topic)
    except discord.HTTPException:
        logger.warning(
            "channel repo pin failed for #%s (%s)", target.name, target.id, exc_info=True
        )
        return (
            f"Couldn't update topic on **#{target.name}** (Discord rejected the edit). "
            f"Set it to `{new_topic}` manually?"
        )

    if not repo_urls:
        logger.info("cleared channel #%s (%s)", target.name, target.id)
        return f"Cleared repo binding on **#{target.name}**."
    logger.info("pinned channel #%s (%s) → %s", target.name, target.id, repo_urls)
    return f"Pinned **#{target.name}** → {_fmt(repo_urls)} (saved in channel topic)."


def topic_from_channel(channel: TopicChannel | None) -> str | None:
    if channel is None:
        return None
    return channel.topic


def _fmt(repos: list[str]) -> str:
    return ", ".join(f"`{compact_repo(r)}`" for r in repos)
