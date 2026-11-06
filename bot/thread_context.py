"""Include prior Discord thread messages in cloud agent prompts (Slack parity)."""
from __future__ import annotations

import logging

import discord

from bot.agent_ref import parse_agent_ref_from_message
from bot.multimodal import AgentPrompt

logger = logging.getLogger(__name__)

MAX_MESSAGES = 35
MAX_CHARS = 14_000
_BOT_SNIPPET = 400
_USER_SNIPPET = 800


async def augment_prompt_with_thread(
    prompt: AgentPrompt,
    *,
    channel: discord.abc.Messageable,
    before_message: discord.Message | None,
    bot_user_id: int,
) -> AgentPrompt:
    """Prepend compact thread history to the user turn."""
    if not prompt.actionable and not prompt.images:
        return prompt

    lines = await _collect_thread_lines(
        channel,
        before_message=before_message,
        bot_user_id=bot_user_id,
    )
    if not lines:
        return prompt

    header = (
        "[Discord thread context — earlier messages in this conversation]\n"
        + "\n".join(lines)
        + "\n\n[Current request]\n"
    )
    combined = header + (prompt.text or "(See attached image(s).)")
    if len(combined) > MAX_CHARS:
        combined = combined[: MAX_CHARS - 1] + "…"
    return AgentPrompt(
        text=combined,
        images=prompt.images,
        actionable=prompt.actionable,
        feedback=prompt.feedback,
    )


async def _collect_thread_lines(
    channel: discord.abc.Messageable,
    *,
    before_message: discord.Message | None,
    bot_user_id: int,
) -> list[str]:
    if not hasattr(channel, "history"):
        return []

    collected: list[tuple[float, str]] = []
    try:
        async for message in channel.history(limit=MAX_MESSAGES, oldest_first=True):
            if before_message is not None and message.id >= before_message.id:
                continue
            line = _format_history_line(message, bot_user_id=bot_user_id)
            if line:
                collected.append((message.created_at.timestamp(), line))
    except discord.HTTPException:
        logger.debug("thread history fetch failed", exc_info=True)
        return []

    collected.sort(key=lambda x: x[0])
    return [line for _, line in collected]


def _format_history_line(message: discord.Message, *, bot_user_id: int) -> str:
    if message.author.bot and message.author.id != bot_user_id:
        return ""

    text = (message.content or "").strip()
    if message.author.id == bot_user_id:
        ref = parse_agent_ref_from_message(message, bot_user_id=bot_user_id)
        if ref and not text:
            return ""
        limit = _BOT_SNIPPET
        role = "Cursor"
    else:
        limit = _USER_SNIPPET
        role = message.author.display_name or "User"

    if not text and message.attachments:
        names = ", ".join(a.filename or "file" for a in message.attachments[:3])
        text = f"(attachments: {names})"
    if not text:
        return ""

    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"{role}: {text}"
