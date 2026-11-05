from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

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
