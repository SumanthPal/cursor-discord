"""What the bot says in a thread: one status message edited in place, then a plain reply."""
from __future__ import annotations

import time

import discord

from bot.agent_ref import ref_trailer
from bot.cursor_agent import TurnResult, friendly_error
from bot.discord_text import DISCORD_CONTENT_MAX, clip_discord, split_discord
from bot.repos import compact_repo

MAX_REPLY_MESSAGES = 4


def working(repo: str | None = None) -> str:
    if repo:
        return f"On it, working in `{compact_repo(repo)}`…"
    return "On it…"


def failed(exc: object) -> str:
    return clip_discord(f"I couldn't get that going: {friendly_error(exc)}")


def reply_chunks(result: TurnResult) -> list[str]:
    """The finished reply, split for Discord, with the agent id as subtext at the end."""
    if result.status == "cancelled":
        body = "Stopped."
    elif result.status == "expired":
        body = "That run timed out before finishing."
    elif result.status == "error":
        body = f"I ran into a problem: {result.error or 'unknown error'}"
        if result.text:
            body += f"\n\n{result.text}"
    else:
        body = result.text or "Done."

    trailer = ref_trailer(result.agent_id)
    chunks = split_discord(body, DISCORD_CONTENT_MAX - len(trailer) - 1) or [""]
    if len(chunks) > MAX_REPLY_MESSAGES:
        chunks = chunks[:MAX_REPLY_MESSAGES]
        more = f"\n… (the rest is at https://cursor.com/agents/{result.agent_id})"
        chunks[-1] = clip_discord(chunks[-1], DISCORD_CONTENT_MAX - len(trailer) - len(more) - 1) + more
    if trailer:
        chunks[-1] = f"{chunks[-1]}\n{trailer}".strip()
    return chunks


def pr_button(pr_url: str | None) -> discord.ui.View | None:
    if not pr_url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="View PR", url=pr_url, style=discord.ButtonStyle.link))
    return view


class StatusMessage:
    """One bot message that shows live activity, edited at most every ``min_interval`` s."""

    def __init__(self, message: discord.Message, *, min_interval: float = 1.5) -> None:
        self.message = message
        self.min_interval = min_interval
        self._header = message.content or working()
        self._last_edit = 0.0
        self._last_body = self._header

    async def activity(self, line: str) -> None:
        body = clip_discord(f"{self._header}\n-# {' '.join(line.split())}")
        now = time.monotonic()
        if body == self._last_body or now - self._last_edit < self.min_interval:
            return
        await self._edit(body)
        self._last_edit = now

    async def finish(self, body: str, *, view: discord.ui.View | None = None) -> None:
        body = clip_discord(body)
        if not await self._edit(body, view=view):
            # Status message gone (deleted?) — the reply still has to land.
            kwargs = {"view": view} if view is not None else {}
            await self.message.channel.send(body, **kwargs)

    async def _edit(self, body: str, view: discord.ui.View | None = None) -> bool:
        try:
            if view is None:
                await self.message.edit(content=body)
            else:
                await self.message.edit(content=body, view=view)
        except discord.HTTPException:
            return False
        self._last_body = body
        return True
