from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import discord

from bot.agent_ref import is_agent_id
from bot.channel_bind import pin_channel_repo
from bot.repo_refs import DISCORD_VALUE_MAX, repo_path

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)


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


class SetupView(discord.ui.View):
    """Admin multi-select: bind this channel to one or more repos."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        *,
        repo_urls: list[str],
        owner_id: int,
        channel: discord.abc.Messageable | None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id
        self.channel = channel
        self._urls = [u.rstrip("/") for u in repo_urls[:25]]
        select = discord.ui.Select(
            placeholder="Repos allowed in this channel…",
            options=[
                discord.SelectOption(
                    label=short_repo_label(url), value=str(i), description=_desc(url)
                )
                for i, url in enumerate(self._urls)
            ],
            min_values=1,
            max_values=len(self._urls),
        )
        select.callback = self._picked  # type: ignore[method-assign]
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.check_admin(interaction.user)
        if denied or interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                denied or "Only the person who ran /setup can pick.", ephemeral=True
            )
            return
        assert isinstance(self.children[0], discord.ui.Select)
        urls = [self._urls[int(v)] for v in self.children[0].values]
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = await pin_channel_repo(self.bot, self.channel, urls, force=True)
        self.stop()
        await interaction.followup.send(note, ephemeral=True)


class AgentControls(discord.ui.View):
    """Legacy cancel/PR buttons — prefer `/cancel` and links in chat replies."""

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
