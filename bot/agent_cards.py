"""Teams-style launch / completion handoff for Discord."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.agent_sessions import agent_dashboard_url
from bot.ui import short_repo_label

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot


def launch_embed(
    *,
    repo: str,
    branch: str | None,
    model: str | None,
    prompt: str,
    agent_id: str | None = None,
) -> discord.Embed:
    ref = branch or "default branch"
    model_label = model or "default"
    desc = prompt.strip()
    if len(desc) > 500:
        desc = desc[:497] + "…"
    embed = discord.Embed(
        title="Cursor cloud agent",
        description=desc or "—",
        color=0x5865F2,
    )
    embed.add_field(name="Repository", value=f"`{short_repo_label(repo)}`", inline=True)
    embed.add_field(name="Branch", value=f"`{ref}`", inline=True)
    embed.add_field(name="Model", value=f"`{model_label}`", inline=True)
    if agent_id:
        embed.set_footer(text=agent_id)
    return embed


def completion_embed(
    *,
    agent_id: str,
    status: str,
    summary: str | None,
    pr_url: str | None,
) -> discord.Embed:
    key = (status or "").lower()
    if key in {"error"}:
        color = 0xED4245
        title = "Agent failed"
    elif key in {"cancelled", "canceled"}:
        color = 0xFEE75C
        title = "Agent cancelled"
    else:
        color = 0x57F287
        title = "Agent finished"
    body = (summary or "").strip()
    if len(body) > 900:
        body = body[:897] + "…"
    embed = discord.Embed(title=title, description=body or "—", color=color)
    embed.set_footer(text=agent_id)
    if pr_url:
        embed.add_field(name="Pull request", value=pr_url, inline=False)
    return embed


class AgentHandoffView(discord.ui.View):
    def __init__(
        self,
        *,
        agent_id: str,
        pr_url: str | None = None,
        repo_urls: list[str] | None = None,
        bot: CursorDiscordBot | None = None,
        relaunch_prompt: str | None = None,
        relaunch_user_id: int | None = None,
        relaunch_channel_id: int | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Open in Web",
                style=discord.ButtonStyle.link,
                url=agent_dashboard_url(agent_id),
            )
        )
        if pr_url:
            self.add_item(
                discord.ui.Button(
                    label="View PR",
                    style=discord.ButtonStyle.link,
                    url=pr_url,
                )
            )
        if (
            bot is not None
            and relaunch_prompt
            and relaunch_user_id is not None
            and relaunch_channel_id is not None
            and repo_urls
        ):
            self.add_item(
                SwitchRepoSelect(
                    bot=bot,
                    agent_id=agent_id,
                    repo_urls=repo_urls[:25],
                    relaunch_prompt=relaunch_prompt,
                    relaunch_user_id=relaunch_user_id,
                    relaunch_channel_id=relaunch_channel_id,
                )
            )


class SwitchRepoSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        bot: CursorDiscordBot,
        agent_id: str,
        repo_urls: list[str],
        relaunch_prompt: str,
        relaunch_user_id: int,
        relaunch_channel_id: int,
    ) -> None:
        self._bot = bot
        self._agent_id = agent_id
        self._prompt = relaunch_prompt
        self._user_id = relaunch_user_id
        self._channel_id = relaunch_channel_id
        options = [
            discord.SelectOption(
                label=short_repo_label(url)[:100],
                value=str(i),
            )
            for i, url in enumerate(repo_urls)
        ]
        super().__init__(
            placeholder="Switch repository…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._urls = repo_urls

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "Only the person who started this agent can switch repo.",
                ephemeral=True,
            )
            return
        idx = int(self.values[0])
        repo = self._urls[idx]
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = interaction.client.cogs.get("AgentCog")
        if cog is None:
            await interaction.followup.send("Bot not ready.", ephemeral=True)
            return
        from bot.multimodal import AgentPrompt

        note = (
            f"Starting a **new** agent on `{short_repo_label(repo)}` with the same request. "
            f"(Prior agent `{self._agent_id}` is unchanged.)"
        )
        await interaction.followup.send(note, ephemeral=True)
        await cog._launch_from_switch(  # type: ignore[attr-defined]
            interaction=interaction,
            repo=repo,
            prompt=AgentPrompt.from_text(self._prompt),
            channel_id=self._channel_id,
        )
