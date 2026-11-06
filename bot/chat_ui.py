from __future__ import annotations

import time

import discord

from bot.agent_ref import (
    ACTIVE_AGENT_STATUSES,
    chat_done,
    chat_progress,
    chat_working,
    ref_spoiler,
)
from bot.discord_text import DISCORD_CONTENT_MAX, clip_discord


def chat_ack(
    *,
    prompt: str,
    repo: str | None = None,
    echo_prompt: bool = True,
) -> str:
    """First bot line when a run starts — echoes the ask unless the thread anchor is the user message."""
    lines: list[str] = []
    if echo_prompt:
        cleaned = " ".join((prompt or "").split())
        if len(cleaned) > 400:
            cleaned = cleaned[:397] + "…"
        if cleaned:
            lines.append(cleaned)
    lines.append(chat_working(repo=repo))
    return clip_discord("\n".join(lines))


def chat_status_body(
    *,
    agent_id: str,
    status: str,
    repo: str | None = None,
    snippet: str | None = None,
    summary: str | None = None,
    run_id: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
) -> str:
    key = (status or "").lower()
    if error:
        key = "error"
        summary = error

    if key in ACTIVE_AGENT_STATUSES:
        if snippet and snippet.strip():
            base = chat_progress(snippet.strip())
        else:
            base = chat_working(repo=repo)
        trailer = ref_spoiler(agent_id, run_id) if agent_id not in {"", "pending"} else ""
        if trailer:
            return clip_discord(f"{base}\n{trailer}")
        return clip_discord(base)

    text = chat_done(
        agent_id=agent_id if agent_id not in {"", "pending"} else "pending",
        status=key,
        summary=summary,
        run_id=run_id,
        pr_url=pr_url,
        repo=repo,
    )
    if key in {"finished", "completed", "done"} and agent_id not in {"", "pending"}:
        hint = "reply here to follow up · `/cancel` to stop"
        if pr_url:
            hint = "reply here to follow up"
        trailer = ref_spoiler(agent_id, run_id)
        if trailer and trailer in text:
            body = text[: text.rfind(trailer)].rstrip()
            combined = f"{body}\n_{hint}_\n{trailer}"
            if len(combined) <= DISCORD_CONTENT_MAX:
                return combined
            room = DISCORD_CONTENT_MAX - len(trailer) - 1
            return clip_discord(body, max(1, room)) + "\n" + trailer
        return clip_discord(f"{text}\n_{hint}_")
    return text


class ChatStatusEditor:
    """Edits one Discord message in place with rate-safe throttling."""

    def __init__(
        self,
        message: discord.Message,
        *,
        min_interval_sec: float = 1.25,
    ) -> None:
        self.message = message
        self.min_interval_sec = min_interval_sec
        self._last_edit = 0.0
        self._last_body = ""

    async def set_content(
        self,
        body: str,
        *,
        force: bool = False,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        body = clip_discord(body)
        signature = (body, embed.to_dict() if embed else None, view is not None)
        if signature == getattr(self, "_last_signature", None) and not force:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_body
            and now - self._last_edit < self.min_interval_sec
        ):
            return
        try:
            await self.message.edit(content=body or None, embed=embed, view=view)
            self._last_body = body
            self._last_signature = signature
            self._last_edit = now
        except discord.HTTPException:
            pass
