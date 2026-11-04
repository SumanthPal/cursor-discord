from __future__ import annotations

import re
import time
from dataclasses import dataclass

import discord

from bot.agent_ref import (
    chat_done,
    chat_failed,
    chat_progress,
    chat_working,
    ref_spoiler,
)
from bot.repo_refs import repo_path

DISCORD_CONTENT_MAX = 2000


def clip_discord(text: str, limit: int = DISCORD_CONTENT_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"

_ACTIVE = frozenset({"starting", "created", "running", "streaming"})


def chat_ack(*, prompt: str, repo: str | None = None) -> str:
    """First bot line when a run starts — echoes the ask, then a short working line."""
    cleaned = " ".join((prompt or "").split())
    if len(cleaned) > 400:
        cleaned = cleaned[:397] + "…"
    lines = [cleaned] if cleaned else []
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

    if key in _ACTIVE:
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
        text = clip_discord(f"{text}\n_{hint}_")
    return text


def format_repo_choice_prompt(urls: list[str], *, task_preview: str) -> str:
    """Natural-language repo disambiguation — user replies with a number or name."""
    task = " ".join((task_preview or "").split())
    if len(task) > 200:
        task = task[:197] + "…"
    lines = [
        "I can work on that — which repo should I use?",
        f"_{task}_" if task else "",
        "",
    ]
    for i, url in enumerate(urls[:25], start=1):
        label = repo_path(url) or url
        lines.append(f"{i}. `{label}`")
    lines.extend(
        [
            "",
            "Reply with the **number** or **repo name** (e.g. `acme/widgets`).",
            "This channel has no default repo yet — your pick can pin it for next time.",
        ]
    )
    return clip_discord("\n".join(line for line in lines if line is not None))


_NUM_RE = re.compile(r"^\s*#?\s*(\d{1,2})\s*$")


def resolve_repo_from_reply(text: str, urls: list[str]) -> str | None:
    """Match user reply to a repo URL, or None if no match."""
    raw = (text or "").strip()
    if not raw or not urls:
        return None

    num = _NUM_RE.match(raw)
    if num:
        idx = int(num.group(1))
        if 1 <= idx <= len(urls):
            return urls[idx - 1]

    needle = raw.lower().strip("`\"'")
    if needle.startswith("http"):
        norm = needle.rstrip("/")
        for url in urls:
            if url.rstrip("/").lower() == norm:
                return url

    best: tuple[int, str] | None = None
    for url in urls:
        label = (repo_path(url) or url).lower()
        if needle == label or needle in label or label.endswith("/" + needle):
            score = len(label) if needle == label else len(needle)
            if best is None or score > best[0]:
                best = (score, url)
    return best[1] if best else None


@dataclass
class PendingRepoLaunch:
    """Waiting for the user to name a repo before starting an agent."""

    agent_prompt: object  # AgentPrompt — avoid circular import
    urls: list[str]
    branch: str | None
    owner_id: int
    channel_id: int


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

    async def set_content(self, body: str, *, force: bool = False) -> None:
        body = clip_discord(body)
        if body == self._last_body:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_body
            and now - self._last_edit < self.min_interval_sec
        ):
            return
        try:
            await self.message.edit(content=body, embed=None, view=None)
            self._last_body = body
            self._last_edit = now
        except discord.HTTPException:
            pass
