from __future__ import annotations

import re
from dataclasses import dataclass

import discord

from bot.repo_refs import repo_path

FOOTER_PREFIX = "cursor-agent:"
AGENT_ID_RE = re.compile(r"^bc-[A-Za-z0-9-]+$")
# Spoiler-hidden machine trailer: ||cursor-agent:bc-… · run:…||
_REF_SPOILER_RE = re.compile(
    rf"\|\|{re.escape(FOOTER_PREFIX)}(bc-[A-Za-z0-9-]+)"
    r"(?: · run:([A-Za-z0-9_-]+))?\|\|"
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
    return (status or "").lower() in {
        "starting",
        "created",
        "running",
        "streaming",
    }


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def ref_spoiler(agent_id: str, run_id: str | None = None) -> str:
    """Hide the bc- id so Discord still carries state without bot chrome."""
    if not is_agent_id(agent_id):
        return ""
    core = f"{FOOTER_PREFIX}{agent_id}"
    if run_id:
        core = f"{core} · run:{run_id}"
    return f"||{core}||"


def parse_ref_spoiler(content: str | None) -> AgentRef | None:
    if not content:
        return None
    match = _REF_SPOILER_RE.search(content)
    if not match:
        return None
    return AgentRef(agent_id=match.group(1), run_id=match.group(2))


def _repo_label(repo: str) -> str:
    return repo_path(repo) or repo


def chat_working(*, repo: str | None = None, agent_id: str | None = None) -> str:
    if repo and repo != "(existing agent)":
        line = f"on it — spinning up in the cloud for `{_repo_label(repo)}`…"
    else:
        line = "on it — working in the cloud…"
    trailer = ref_spoiler(agent_id) if agent_id else ""
    return _truncate(f"{line}\n{trailer}".strip() if trailer else line, 2000)


def chat_progress(
    *,
    snippet: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> str:
    body = (snippet or "").strip()
    if body:
        line = f"still working…\n_{_truncate(body, 400)}_"
    else:
        line = "still working… (clone / setup can take a minute)"
    trailer = ref_spoiler(agent_id, run_id) if agent_id else ""
    return _truncate(f"{line}\n{trailer}".strip() if trailer else line, 2000)


def chat_done(
    *,
    agent_id: str,
    status: str,
    summary: str | None,
    run_id: str | None = None,
    pr_url: str | None = None,
    repo: str | None = None,
) -> str:
    """Final (or error) message — reads like a person, id tucked in a spoiler."""
    key = (status or "").lower()
    body = (summary or "").strip()

    if key in {"error"}:
        head = f"hit a snag{': ' + body if body else '.'}"
        body = ""
        cue = "retry with `/ask` or another reply."
    elif key in {"cancelled", "canceled"}:
        head = "cancelled."
        body = ""
        cue = "say what you want next, or `/ask` for a fresh agent."
    elif body:
        head = "done."
        cue = "**your turn** — reply in this thread to steer (no @ needed)."
    else:
        head = "done."
        body = ""
        cue = "**your turn** — reply in this thread to steer (no @ needed)."

    parts: list[str] = [head]
    if body:
        parts.append(_truncate(body, 1600))
    if pr_url:
        parts.append(pr_url)
    parts.append(cue)

    trailer = ref_spoiler(agent_id, run_id)
    if trailer:
        parts.append(trailer)
    elif repo and repo != "(existing agent)":
        parts.append(f"`{_repo_label(repo)}`")

    text = "\n".join(p for p in parts if p)
    return _truncate(text, 2000) or "done."


def chat_failed(exc: object) -> str:
    return _truncate(f"couldn't start — {exc}", 2000)


# --- legacy embed support (older status cards still resolve in threads) ---


def build_starter_embed(
    *,
    agent_id: str,
    prompt: str,
    project: str | None,
    repo: str,
    status: str = "starting",
    summary: str | None = None,
    run_id: str | None = None,
    pr_url: str | None = None,
    branch: str | None = None,
    tip: str | None = None,
) -> discord.Embed:
    """Thin leftover for anything still calling embeds; prefer chat_* helpers."""
    _ = (prompt, project, tip, branch)  # unused — kept for call-site compat
    color = discord.Color.dark_grey()
    if _is_active(status):
        color = discord.Color.blurple()
    elif (status or "").lower() in {"finished", "completed"}:
        color = discord.Color.green()
    elif (status or "").lower() == "error":
        color = discord.Color.red()
    embed = discord.Embed(
        description=chat_done(
            agent_id=agent_id if is_agent_id(agent_id) else "pending",
            status=status,
            summary=summary or chat_working(repo=repo),
            run_id=run_id,
            pr_url=pr_url,
            repo=repo,
        )[:4096],
        color=color,
    )
    footer = f"{FOOTER_PREFIX}{agent_id}"
    if run_id:
        footer = f"{footer} · run:{run_id}"
    embed.set_footer(text=_truncate(footer, 180))
    return embed


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


def _field_value(embed: discord.Embed, name: str) -> str | None:
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None
