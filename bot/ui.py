from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import discord

from bot.agent_ref import is_agent_id
from bot.projects import Project

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

OnRepoPicked = Callable[[discord.Interaction, Project], Awaitable[None]]


def short_repo_label(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    label = path or url
    return label[:100]


class RepoPickerView(discord.ui.View):
    """Pick a Cursor-connected repo in Discord (no YAML required)."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        *,
        prompt: str,
        repo_urls: list[str],
        branch: str | None,
        on_picked: OnRepoPicked,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.prompt = prompt
        self.branch = branch
        self.on_picked = on_picked

        options: list[discord.SelectOption] = []
        for url in repo_urls[:25]:
            label = short_repo_label(url)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=url[:100],
                    description=url[:100],
                )
            )
        select = discord.ui.Select(
            placeholder="Choose a Cursor-connected repo…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.children[0], discord.ui.Select)
        url = self.children[0].values[0]
        project = Project(
            alias="_explicit",
            repo=url,
            branch=(self.branch or "main").strip() or "main",
            model=self.bot.settings.cursor_model,
        )
        await interaction.response.defer(ephemeral=False, thinking=True)
        self.stop()
        await self.on_picked(interaction, project)


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
                label="Cancel run",
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

        if is_agent_id(agent_id) and not running:
            status_btn = discord.ui.Button(
                label="Status",
                style=discord.ButtonStyle.secondary,
                custom_id=f"cursor:status:{agent_id}"[:100],
            )
            status_btn.callback = self._status  # type: ignore[method-assign]
            self.add_item(status_btn)

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

    async def _status(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild, consume_rate=False
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            info = await self.bot.cursor.status(self.agent_id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Status failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"**{info.get('name') or 'Agent'}** `{info['agent_id']}`\n"
            f"Status: {info.get('status') or 'unknown'}\n"
            f"{info.get('summary') or ''}".strip(),
            ephemeral=True,
        )


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
    return AgentControls(
        bot, agent_id=agent_id or "pending", running=running, pr_url=pr_url
    )
