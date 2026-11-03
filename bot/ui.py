from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
import discord

from bot.agent_ref import is_agent_id
from bot.projects import Project, parse_channel_repo, set_channel_repo_topic
from bot.repo_refs import DISCORD_VALUE_MAX, repo_path

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

OnRepoPicked = Callable[[discord.Interaction, Project], Awaitable[None]]


def short_repo_label(url: str) -> str:
    """Display label only — may truncate. Never use as a clone URL."""
    label = repo_path(url)
    if len(label) <= DISCORD_VALUE_MAX:
        return label
    return label[: DISCORD_VALUE_MAX - 1] + "…"


def _desc(url: str) -> str:
    if len(url) <= DISCORD_VALUE_MAX:
        return url
    return url[: DISCORD_VALUE_MAX - 1] + "…"


class RepoPickerView(discord.ui.View):
    """Pick a Cursor-connected repo. First pick can silently pin the channel topic."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        *,
        prompt: str,
        repo_urls: list[str],
        branch: str | None,
        on_picked: OnRepoPicked,
        owner_id: int,
        channel: discord.abc.Messageable | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.prompt = prompt
        self.branch = branch
        self.on_picked = on_picked
        self.owner_id = owner_id
        self.channel = _topic_channel(channel)
        self._urls = [u.rstrip("/") for u in repo_urls[:25]]

        options: list[discord.SelectOption] = []
        for i, url in enumerate(self._urls):
            options.append(
                discord.SelectOption(
                    label=short_repo_label(url),
                    value=str(i),
                    description=_desc(url),
                )
            )
        select = discord.ui.Select(
            placeholder="Choose a repo…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who started this can pick a repo.",
                ephemeral=True,
            )
            return
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        assert isinstance(self.children[0], discord.ui.Select)
        key = self.children[0].values[0]
        try:
            url = self._urls[int(key)]
        except (ValueError, IndexError) as exc:
            await interaction.response.send_message(
                f"Invalid repo selection: {exc}", ephemeral=True
            )
            return

        # Ack first — topic edit can exceed Discord's 3s interaction window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._maybe_pin(url)

        project = Project(
            alias="_explicit",
            repo=url,
            branch=(self.branch or "").strip() or None,
            model=self.bot.settings.cursor_model,
        )
        self.stop()
        await self.on_picked(interaction, project)

    async def _maybe_pin(self, repo_url: str) -> None:
        """If channel has no cursor-repo yet and we can edit topics, pin silently."""
        channel = self.channel
        if channel is None or parse_channel_repo(channel.topic):
            return
        if not channel.permissions_for(channel.guild.me).manage_channels:
            return
        try:
            await channel.edit(topic=set_channel_repo_topic(channel.topic, repo_url))
        except discord.HTTPException:
            logger.warning("silent channel repo pin failed", exc_info=True)


def _topic_channel(
    channel: discord.abc.Messageable | None,
) -> discord.TextChannel | None:
    """Channel whose topic we pin — parent text channel when started from a thread."""
    if isinstance(channel, discord.TextChannel):
        return channel
    if isinstance(channel, discord.Thread) and isinstance(
        channel.parent, discord.TextChannel
    ):
        return channel.parent
    return None


class AgentControls(discord.ui.View):
    """Buttons under agent status embeds. Not LLM — plain Discord UI."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        *,
        agent_id: str,
        running: bool = True,
        pr_url: str | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.agent_id = agent_id

        if running and is_agent_id(agent_id):
            cancel = discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.danger,
                custom_id=f"cursor:cancel:{agent_id}"[:100],
            )
            cancel.callback = self._cancel  # type: ignore[method-assign]
            self.add_item(cancel)

        if pr_url:
            self.add_item(
                discord.ui.Button(
                    label="Open PR",
                    style=discord.ButtonStyle.link,
                    url=pr_url,
                )
            )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await self.bot.cursor.cancel(self.agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cancel button failed")
            await interaction.followup.send(f"Cancel failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(msg, ephemeral=True)


def controls_for(
    bot: CursorDiscordBot,
    *,
    agent_id: str,
    status: str,
    pr_url: str | None = None,
) -> AgentControls | None:
    running = (status or "").lower() in {
        "starting",
        "created",
        "running",
        "streaming",
    }
    if not is_agent_id(agent_id) and not pr_url:
        return None
    # Only show chrome while running (Cancel) or when there's a PR link.
    if not running and not pr_url:
        return None
    return AgentControls(
        bot, agent_id=agent_id or "pending", running=running, pr_url=pr_url
    )
