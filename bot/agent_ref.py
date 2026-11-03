from __future__ import annotations

import re
from dataclasses import dataclass

import discord

FOOTER_PREFIX = "cursor-agent:"
AGENT_ID_RE = re.compile(r"^bc-[A-Za-z0-9-]+$")

# Internal status → (label, color, title)
STATUS_UI: dict[str, tuple[str, discord.Color, str]] = {
    "starting": ("Queued…", discord.Color.light_grey(), "Starting Cursor agent"),
    "created": ("Agent ready", discord.Color.blurple(), "Starting Cursor agent"),
    "running": ("Working…", discord.Color.blurple(), "Cursor is working"),
    "streaming": ("Working…", discord.Color.blurple(), "Cursor is working"),
    "finished": ("Done", discord.Color.green(), "Done"),
    "completed": ("Done", discord.Color.green(), "Done"),
    "error": ("Failed", discord.Color.red(), "Something went wrong"),
    "cancelled": ("Cancelled", discord.Color.dark_grey(), "Cancelled"),
    "canceled": ("Cancelled", discord.Color.dark_grey(), "Cancelled"),
}


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


def status_label(status: str) -> str:
    key = (status or "").lower()
    if key in STATUS_UI:
        return STATUS_UI[key][0]
    return status.replace("_", " ").title() or "Unknown"


def status_color(status: str) -> discord.Color:
    key = (status or "").lower()
    if key in STATUS_UI:
        return STATUS_UI[key][1]
    return discord.Color.blurple()


def status_title(status: str) -> str:
    key = (status or "").lower()
    if key in STATUS_UI:
        return STATUS_UI[key][2]
    return "Cursor cloud agent"


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
    embed = discord.Embed(
        title=status_title(status),
        description=_description(prompt, tip=tip, status=status),
        color=status_color(status),
    )
    embed.add_field(name="Status", value=status_label(status), inline=True)
    if project:
        embed.add_field(name="Project", value=project, inline=True)
    if repo and repo != "(existing agent)":
        embed.add_field(name="Repo", value=_truncate(repo, 200), inline=False)
    if is_agent_id(agent_id):
        embed.add_field(name="Agent", value=f"`{agent_id}`", inline=False)
    elif agent_id and agent_id != "pending":
        embed.add_field(name="Agent", value=f"`{agent_id}`", inline=False)
    if branch:
        embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    if pr_url:
        embed.add_field(name="Pull request", value=pr_url, inline=False)
    if summary:
        embed.add_field(
            name="Update" if _is_active(status) else "Summary",
            value=_truncate(summary, 900),
            inline=False,
        )
    # Keep run_id machine-readable but out of the main chrome
    footer = f"{FOOTER_PREFIX}{agent_id}"
    if run_id:
        footer = f"{footer} · run:{run_id}"
    embed.set_footer(text=_truncate(footer, 180))
    return embed


def update_embed_status(
    embed: discord.Embed,
    *,
    status: str,
    summary: str | None = None,
    run_id: str | None = None,
    pr_url: str | None = None,
    branch: str | None = None,
    prompt: str | None = None,
) -> discord.Embed:
    embed.title = status_title(status)
    embed.color = status_color(status)
    if prompt is not None:
        tip = _tip_for_status(status)
        embed.description = _description(prompt, tip=tip, status=status)
    else:
        # Refresh tip line if description already structured
        tip = _tip_for_status(status)
        if embed.description and tip:
            # leave prompt body; append/replace tip via rebuild if we can
            pass
    _set_field(embed, "Status", status_label(status), inline=True)
    if run_id and embed.footer and embed.footer.text:
        agent_part = embed.footer.text.split(" · ")[0]
        embed.set_footer(text=_truncate(f"{agent_part} · run:{run_id}", 180))
    if summary is not None:
        label = "Update" if _is_active(status) else "Summary"
        # Rename old Result/Update fields
        _rename_or_set(embed, ("Update", "Summary", "result", "Result"), label, summary)
    if branch:
        _set_field(embed, "Branch", f"`{branch}`", inline=True)
    if pr_url:
        _set_field(embed, "Pull request", pr_url, inline=False)
    return embed


def parse_agent_ref_from_message(
    message: discord.Message, *, bot_user_id: int
) -> AgentRef | None:
    if message.author.id != bot_user_id:
        return None
    if not message.embeds:
        return None
    return parse_agent_ref_from_embed(message.embeds[0])


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
    if isinstance(channel, discord.Thread):
        try:
            starter = channel.starter_message
            if starter is None:
                starter = await channel.fetch_message(channel.id)
            ref = parse_agent_ref_from_message(starter, bot_user_id=bot_user_id)
            if ref:
                return ref
        except (discord.NotFound, discord.HTTPException, AttributeError):
            pass

    async for message in channel.history(limit=limit, oldest_first=True):
        ref = parse_agent_ref_from_message(message, bot_user_id=bot_user_id)
        if ref:
            return ref
    async for message in channel.history(limit=limit, oldest_first=False):
        ref = parse_agent_ref_from_message(message, bot_user_id=bot_user_id)
        if ref:
            return ref
    return None


def _description(prompt: str, *, tip: str | None, status: str) -> str:
    tip = tip if tip is not None else _tip_for_status(status)
    parts = [f"**You asked**\n{_truncate(prompt, 350)}"]
    if tip:
        parts.append(f"\n{_truncate(tip, 200)}")
    return "\n".join(parts)


def _tip_for_status(status: str) -> str:
    if _is_active(status):
        return "_Working in the cloud — reply in this thread when it’s done to continue._"
    if (status or "").lower() in {"error", "cancelled", "canceled"}:
        return "_You can retry with `/agent` or `/talk`._"
    return "_Reply in this thread to send a follow-up to the same agent._"


def _is_active(status: str) -> bool:
    return (status or "").lower() in {
        "starting",
        "created",
        "running",
        "streaming",
    }


def _field_value(embed: discord.Embed, name: str) -> str | None:
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None


def _set_field(
    embed: discord.Embed, name: str, value: str, *, inline: bool
) -> None:
    for i, field in enumerate(embed.fields):
        if field.name == name:
            embed.set_field_at(i, name=name, value=_truncate(value, 1024), inline=inline)
            return
    embed.add_field(name=name, value=_truncate(value, 1024), inline=inline)


def _rename_or_set(
    embed: discord.Embed,
    old_names: tuple[str, ...],
    new_name: str,
    value: str,
) -> None:
    value = _truncate(value, 900)
    for i, field in enumerate(embed.fields):
        if field.name in old_names or field.name == new_name:
            embed.set_field_at(i, name=new_name, value=value, inline=False)
            return
    embed.add_field(name=new_name, value=value, inline=False)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
