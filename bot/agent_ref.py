from __future__ import annotations

import re
from dataclasses import dataclass

import discord

from bot.repo_refs import repo_path

FOOTER_PREFIX = "cursor-agent:"
AGENT_ID_RE = re.compile(r"^bc-[A-Za-z0-9-]+$")
ACTIVE_AGENT_STATUSES = frozenset(
    {"starting", "created", "running", "streaming"}
)
# Spoiler (legacy) or Discord subtext trailer carrying the bc- id.
_REF_TRAILER_RE = re.compile(
    rf"(?:\|\||-#\s+)?(?:{re.escape(FOOTER_PREFIX)})?`?(bc-[A-Za-z0-9-]+)`?"
    r"(?: · (?:run:)?`?([A-Za-z0-9_-]+)`?)?(?:\|\|)?"
)


@dataclass(frozen=True)
class AgentRef:
    agent_id: str
    project: str | None = None
    repo: str | None = None
    run_id: str | None = None


def is_agent_id(value: str | None) -> bool:
    if not value:
        return False
    return bool(AGENT_ID_RE.match(value.strip()))


def _is_active(status: str) -> bool:
    return (status or "").lower() in ACTIVE_AGENT_STATUSES


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def ref_trailer(agent_id: str, run_id: str | None = None) -> str:
    """Small always-visible Discord subtext carrying the bc- id."""
    if not is_agent_id(agent_id):
        return ""
    bit = f"`{agent_id}`"
    if run_id:
        bit = f"{bit} · `{run_id}`"
    return f"-# {bit}"


def ref_spoiler(agent_id: str, run_id: str | None = None) -> str:
    """Back-compat alias — prefer visible subtext over hover spoilers."""
    return ref_trailer(agent_id, run_id)


def parse_ref_spoiler(content: str | None) -> AgentRef | None:
    if not content:
        return None
    match = _REF_TRAILER_RE.search(content)
    if not match:
        return None
    return AgentRef(agent_id=match.group(1), run_id=match.group(2))


def _repo_label(repo: str) -> str:
    return repo_path(repo) or repo


def chat_working(*, repo: str | None = None, status: str | None = None) -> str:
    key = (status or "").lower()
    if key in {"starting", "created"}:
        verb = "starting"
    elif key in {"running", "streaming"}:
        verb = "working"
    else:
        verb = "working"
    if repo and repo != "(existing agent)":
        return f"{verb} · `{_repo_label(repo)}`"
    return f"{verb}…"


def chat_progress(snippet: str) -> str:
    return _truncate(snippet, 1900)


def chat_done(
    *,
    agent_id: str,
    status: str,
    summary: str | None,
    run_id: str | None = None,
    pr_url: str | None = None,
    repo: str | None = None,
) -> str:
    """Final (or live) message — id as small subtext, never hover-only."""
    key = (status or "").lower()
    body = (summary or "").strip()

    if key in {"error"}:
        head = f"hit a snag{': ' + body if body else '.'}"
        body = ""
    elif key in {"cancelled", "canceled"}:
        head = "cancelled."
        body = ""
    elif _is_active(key) and not body:
        head = chat_working(repo=repo, status=key)
    elif body:
        head = ""
    else:
        head = "done."

    parts: list[str] = []
    if head:
        parts.append(head)
    if body:
        parts.append(_truncate(body, 1800))
    if pr_url:
        parts.append(pr_url)

    trailer = ref_trailer(agent_id, run_id)
    if trailer:
        parts.append(trailer)
    elif repo and repo != "(existing agent)":
        parts.append(f"`{_repo_label(repo)}`")

    text = "\n".join(p for p in parts if p)
    return _truncate(text, 2000) or "done."


def chat_failed(exc: object) -> str:
    return _truncate(f"couldn't start — {exc}", 2000)


# --- legacy embed support (older status cards still resolve in threads) ---


def parse_agent_ref_from_message(
    message: discord.Message, *, bot_user_id: int
) -> AgentRef | None:
    if message.author.id != bot_user_id:
        return None
    spoiler = parse_ref_spoiler(message.content)
    if spoiler:
        return spoiler
    if message.embeds:
        return parse_agent_ref_from_embed(message.embeds[0])
    return None


def parse_agent_ref_from_embed(embed: discord.Embed) -> AgentRef | None:
    agent_id = None
    run_id = None
    if embed.footer and embed.footer.text:
        text = embed.footer.text
        if text.startswith(FOOTER_PREFIX):
            rest = text[len(FOOTER_PREFIX) :].strip()
            agent_part, _, run_part = rest.partition(" · ")
            agent_id = agent_part.strip()
            if run_part.startswith("run:"):
                run_id = run_part[4:].strip()
    if not agent_id and embed.description:
        spoiler = parse_ref_spoiler(embed.description)
        if spoiler:
            return spoiler
    if not agent_id:
        for name in ("Agent", "agent_id"):
            raw = _field_value(embed, name)
            if raw:
                agent_id = raw.strip().strip("`")
                break
    if not is_agent_id(agent_id):
        return None
    project = _field_value(embed, "Project") or _field_value(embed, "project")
    repo = _field_value(embed, "Repo") or _field_value(embed, "repo")
    if not run_id:
        raw_run = _field_value(embed, "run_id")
        if raw_run:
            run_id = raw_run.strip().strip("`")
    return AgentRef(
        agent_id=agent_id,  # type: ignore[arg-type]
        project=project,
        repo=repo,
        run_id=run_id,
    )


async def find_agent_ref_in_channel(
    channel: discord.abc.Messageable, *, bot_user_id: int, limit: int = 50
) -> AgentRef | None:
    async for message in channel.history(limit=limit, oldest_first=False):
        ref = parse_agent_ref_from_message(message, bot_user_id=bot_user_id)
        if ref:
            return ref
    return None


def thread_agent_ref(
    thread_id: int | None,
    thread_agents: dict[int, AgentRef] | None,
) -> AgentRef | None:
    if thread_id is None or not thread_agents:
        return None
    return thread_agents.get(thread_id)


async def resolve_agent_ref_in_channel(
    channel: discord.abc.Messageable,
    *,
    bot_user_id: int,
    thread_agents: dict[int, AgentRef] | None = None,
    limit: int = 50,
) -> AgentRef | None:
    """Prefer live thread binding (active runs hide ids in message text)."""
    if isinstance(channel, discord.Thread):
        cached = thread_agent_ref(channel.id, thread_agents)
        if cached is not None:
            return cached
    return await find_agent_ref_in_channel(
        channel, bot_user_id=bot_user_id, limit=limit
    )


def _field_value(embed: discord.Embed, name: str) -> str | None:
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None
