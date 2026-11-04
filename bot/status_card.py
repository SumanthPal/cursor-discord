"""Shared Discord status embed updates during agent runs."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord

from bot.agent_ref import build_starter_embed, chat_progress
from bot.ui import controls_for

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

# Avoid Discord rate limits during streaming status ticks.
_MIN_EDIT_INTERVAL_SEC = 1.5
_last_edit_monotonic: dict[int, float] = {}


def run_status_summary(data: dict) -> str | None:
    if data.get("error"):
        return str(data["error"])
    raw = data.get("snippet") or data.get("result")
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if data.get("snippet"):
        return chat_progress(text)
    return text


def build_run_status_card(
    *,
    bot: CursorDiscordBot,
    agent_id: str,
    prompt: str,
    project_alias: str | None,
    repo: str,
    state: str,
    data: dict,
) -> tuple[discord.Embed, discord.ui.View]:
    resolved_id = str(data.get("agent_id") or agent_id or "pending")
    summary = run_status_summary(data)
    status = state
    if data.get("error"):
        status = "error"
    embed = build_starter_embed(
        agent_id=resolved_id,
        prompt=prompt,
        project=project_alias,
        repo=repo,
        status=status,
        run_id=data.get("run_id"),
        summary=summary,
        pr_url=data.get("pr_url"),
        branch=data.get("branch"),
    )
    view = controls_for(
        bot,
        agent_id=resolved_id,
        status=status,
        pr_url=data.get("pr_url"),
    )
    return embed, view


async def edit_status_card(
    status_message: discord.Message,
    *,
    embed: discord.Embed,
    view: discord.ui.View,
    force: bool = False,
) -> bool:
    """Edit the status message; throttle unless ``force``. Returns True if edited."""
    now = time.monotonic()
    msg_id = status_message.id
    if not force:
        last = _last_edit_monotonic.get(msg_id, 0.0)
        if now - last < _MIN_EDIT_INTERVAL_SEC:
            return False
    try:
        await status_message.edit(embed=embed, view=view)
    except discord.HTTPException:
        logger.warning("failed to edit status message", exc_info=True)
        return False
    _last_edit_monotonic[msg_id] = now
    return True
