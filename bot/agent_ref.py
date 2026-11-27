"""The agent id lives in a thread as small subtext on the bot's finished replies.

That's how a thread maps back to its Cursor agent after a redeploy (no database).
"""
from __future__ import annotations

import re

import discord

AGENT_ID_RE = re.compile(r"^bc-[A-Za-z0-9-]+$")
# Current `-# `bc-…`` subtext, plus older spoiler / `cursor-agent:` forms.
_REF_RE = re.compile(r"(?:\|\||-#\s+)?(?:cursor-agent:)?`?(bc-[A-Za-z0-9-]+)`?")


def is_agent_id(value: str | None) -> bool:
    return bool(value and AGENT_ID_RE.match(value.strip()))


def ref_trailer(agent_id: str) -> str:
    return f"-# `{agent_id}`" if is_agent_id(agent_id) else ""


def parse_ref(content: str | None) -> str | None:
    match = _REF_RE.search(content or "")
    return match.group(1) if match else None


async def find_agent_in_thread(
    thread: discord.Thread, *, bot_user_id: int, limit: int = 50
) -> str | None:
    """Most recent agent id the bot left in this thread, if any."""
    async for message in thread.history(limit=limit):
        if message.author.id == bot_user_id:
            agent_id = parse_ref(message.content)
            if agent_id:
                return agent_id
    return None
