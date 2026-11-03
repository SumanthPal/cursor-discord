from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.agent_ref import (
    build_starter_embed,
    find_agent_ref_in_channel,
    is_agent_id,
)
from bot.projects import Project, set_channel_project_topic
from bot.ui import controls_for

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

HELP_TEXT = """**Cursor Discord — quick guide**

**Start work**
• `/agent prompt:…` plus `project:` or `repo:`
• Or `@bot …` in a channel bound with `/project bind`

**Continue**
• Reply in the agent’s thread (same cloud agent)
• Or `/talk agent_id:bc-… prompt:…`

**Controls**
• `/status` · `/cancel` · buttons on the status card
• Full transcript lives in Cursor’s cloud agent UI — Discord only shows progress + summary

Bot chrome (embeds/buttons) is template UI. Only the agent **Summary** text comes from the model.
"""


class AgentCog(commands.Cog):
    def __init__(self, bot: CursorDiscordBot) -> None:
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.exception("slash error: %s", error)
        msg = f"Error: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="help", description="How to use the Cursor Discord bot")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)

    @app_commands.command(name="agent", description="Create a Cursor cloud agent")
    @app_commands.describe(
        prompt="What the agent should do",
        project="Alias from projects registry",
        repo="owner/repo or full git URL (overrides project)",
        branch="Starting branch (default from project or main)",
    )
    async def agent_cmd(
        self,
        interaction: discord.Interaction,
        prompt: str,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        resolved = self._resolve_project(
            project=project,
            repo=repo,
            branch=branch,
            channel=interaction.channel,
        )
        if resolved is None:
            await interaction.response.send_message(
                "Could not resolve a repo. Pass `project:` / `repo:`, bind the "
                "channel with `/project bind`, or set a registry default.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        await self._launch_new_agent(
            interaction=interaction,
            prompt=prompt,
            project=resolved,
            announce_channel=interaction.channel,
        )

    @app_commands.command(
        name="talk", description="Send a follow-up to an existing cloud agent"
    )
    @app_commands.describe(
        agent_id="Cloud agent id (bc-…)",
        prompt="Follow-up instruction",
    )
    async def talk_cmd(
        self,
        interaction: discord.Interaction,
        agent_id: str,
        prompt: str,
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        if not is_agent_id(agent_id):
            await interaction.response.send_message(
                "agent_id must look like `bc-…`", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        await self._follow_up(
            interaction=interaction,
            agent_id=agent_id.strip(),
            prompt=prompt,
            repo="(existing agent)",
            project_alias=None,
        )

    @app_commands.command(name="status", description="Show cloud agent status")
    @app_commands.describe(agent_id="Optional agent id; otherwise use this thread")
    async def status_cmd(
        self,
        interaction: discord.Interaction,
        agent_id: str | None = None,
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild, consume_rate=False
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        resolved_id = agent_id.strip() if agent_id else None
        if not resolved_id:
            assert self.bot.user is not None
            ref = await find_agent_ref_in_channel(
                interaction.channel, bot_user_id=self.bot.user.id
            )
            if not ref:
                await interaction.followup.send(
                    "No agent id found. Pass `agent_id` or run inside an agent thread.",
                    ephemeral=True,
                )
                return
            resolved_id = ref.agent_id

        try:
            info = await self.bot.cursor.status(resolved_id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Status failed: {exc}", ephemeral=True)
            return

        lines = [
            f"**agent_id:** `{info['agent_id']}`",
            f"**status:** {info.get('status') or 'unknown'}",
            f"**name:** {info.get('name') or '—'}",
            f"**summary:** {info.get('summary') or '—'}",
        ]
        if info.get("repos"):
            lines.append("**repos:** " + ", ".join(info["repos"]))
        if info.get("active_run_id"):
            lines.append(f"**active_run:** `{info['active_run_id']}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel an active cloud agent run")
    @app_commands.describe(agent_id="Optional agent id; otherwise use this thread")
    async def cancel_cmd(
        self,
        interaction: discord.Interaction,
        agent_id: str | None = None,
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        resolved_id = agent_id.strip() if agent_id else None
        run_id = None
        if not resolved_id:
            assert self.bot.user is not None
            ref = await find_agent_ref_in_channel(
                interaction.channel, bot_user_id=self.bot.user.id
            )
            if not ref:
                await interaction.followup.send(
                    "No agent id found. Pass `agent_id` or run inside an agent thread.",
                    ephemeral=True,
                )
                return
            resolved_id = ref.agent_id
            run_id = ref.run_id

        try:
            msg = await self.bot.cursor.cancel(resolved_id, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"Cancel failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(msg, ephemeral=True)

    project = app_commands.Group(name="project", description="Project registry helpers")

    @project.command(name="list", description="List configured project aliases")
    async def project_list(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild, consume_rate=False
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        aliases = self.bot.registry.aliases()
        if not aliases:
            await interaction.response.send_message(
                "No projects configured. Set PROJECTS_JSON or mount projects.yaml, "
                "or pass `repo:` on `/agent`.",
                ephemeral=True,
            )
            return
        default = self.bot.registry.default
        lines = []
        for alias in aliases:
            p = self.bot.registry.get(alias)
            assert p is not None
            mark = " (default)" if alias == default else ""
            lines.append(f"- **{alias}**{mark}: `{p.repo}` @ `{p.branch}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @project.command(
        name="bind",
        description="Bind this channel to a project alias via channel topic",
    )
    @app_commands.describe(alias="Project alias from the registry")
    async def project_bind(
        self, interaction: discord.Interaction, alias: str
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        alias_l = alias.strip().lower()
        if not self.bot.registry.get(alias_l):
            await interaction.response.send_message(
                f"Unknown project `{alias_l}`. Use `/project list`.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Bind only works on text channels.", ephemeral=True
            )
            return
        if not channel.permissions_for(channel.guild.me).manage_channels:
            await interaction.response.send_message(
                "I need **Manage Channels** to update the topic.", ephemeral=True
            )
            return
        new_topic = set_channel_project_topic(channel.topic, alias_l)
        await channel.edit(topic=new_topic)
        await interaction.response.send_message(
            f"Bound #{channel.name} → `{alias_l}` "
            f"(topic marker `cursor-project:{alias_l}`).",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.bot.user is None:
            return

        # Thread follow-ups to an existing agent
        if isinstance(message.channel, discord.Thread):
            ref = await find_agent_ref_in_channel(
                message.channel, bot_user_id=self.bot.user.id
            )
            if ref and not _is_bot_mention_only(message, self.bot.user):
                # Ignore slash-induced empties; require non-empty text
                prompt = message.content.strip()
                if prompt and not message.content.startswith("/"):
                    denied = self.bot.policy.authorize(
                        user=message.author, guild=message.guild
                    )
                    if denied:
                        await message.reply(denied, mention_author=False)
                        return
                    await self._follow_up_message(
                        message=message,
                        agent_id=ref.agent_id,
                        prompt=prompt,
                        repo=ref.repo or "(existing agent)",
                        project_alias=ref.project,
                    )
                    return

        if self.bot.user not in message.mentions:
            return
        prompt = _strip_mention(message.content, self.bot.user)
        if not prompt:
            await message.reply(
                "Usage: `@bot <prompt>` in a project-bound channel, "
                "or `/agent prompt:… project:…`",
                mention_author=False,
            )
            return

        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            await message.reply(denied, mention_author=False)
            return

        resolved = self._resolve_project(
            project=None,
            repo=None,
            branch=None,
            channel=message.channel,
        )
        if resolved is None:
            await message.reply(
                "No project resolved. Use `/project bind`, `/agent` with "
                "`project:`/`repo:`, or set a registry default.",
                mention_author=False,
            )
            return

        await self._launch_new_agent_from_message(
            message=message, prompt=prompt, project=resolved
        )

    def _resolve_project(
        self,
        *,
        project: str | None,
        repo: str | None,
        branch: str | None,
        channel: discord.abc.Messageable | None,
    ) -> Project | None:
        topic = None
        if isinstance(channel, discord.TextChannel):
            topic = channel.topic
        elif isinstance(channel, discord.Thread) and isinstance(
            channel.parent, discord.TextChannel
        ):
            topic = channel.parent.topic
        return self.bot.registry.resolve(
            project=project,
            repo=repo,
            branch=branch,
            channel_topic=topic,
            default_model=self.bot.settings.cursor_model,
        )

    async def _launch_new_agent(
        self,
        *,
        interaction: discord.Interaction,
        prompt: str,
        project: Project,
        announce_channel: discord.abc.Messageable | None,
    ) -> None:
        channel = announce_channel or interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Need a text channel or thread.")
            return

        embed = build_starter_embed(
            agent_id="pending",
            prompt=prompt,
            project=project.alias if project.alias != "_explicit" else None,
            repo=project.repo,
            status="starting",
        )
        placeholder = await interaction.followup.send(embed=embed)
        thread = await self._ensure_thread(channel, placeholder, prompt)
        await thread.send(
            "Thread linked to this run. **Reply here** after it finishes to continue."
        )
        await self._run_create(
            status_message=placeholder,
            thread=thread,
            prompt=prompt,
            project=project,
        )

    async def _launch_new_agent_from_message(
        self,
        *,
        message: discord.Message,
        prompt: str,
        project: Project,
    ) -> None:
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await message.reply("Need a text channel.", mention_author=False)
            return
        placeholder = await message.reply(
            embed=build_starter_embed(
                agent_id="pending",
                prompt=prompt,
                project=project.alias if project.alias != "_explicit" else None,
                repo=project.repo,
                status="starting",
            ),
            mention_author=False,
        )
        thread = await self._ensure_thread(channel, placeholder, prompt)
        if thread.id != getattr(channel, "id", None):
            await thread.send(
                "Thread linked to this run. **Reply here** after it finishes to continue."
            )
        await self._run_create(
            status_message=placeholder,
            thread=thread,
            prompt=prompt,
            project=project,
        )

    async def _ensure_thread(
        self,
        channel: discord.TextChannel | discord.Thread,
        starter: discord.Message,
        prompt: str,
    ) -> discord.Thread:
        if isinstance(channel, discord.Thread):
            return channel
        name = _thread_name(prompt)
        try:
            return await starter.create_thread(name=name)
        except discord.HTTPException:
            return await channel.create_thread(
                name=name, message=starter, type=discord.ChannelType.public_thread
            )

    async def _run_create(
        self,
        *,
        status_message: discord.Message,
        thread: discord.Thread,
        prompt: str,
        project: Project,
    ) -> None:
        embed = status_message.embeds[0] if status_message.embeds else None

        async def on_status(state: str, data: dict) -> None:
            nonlocal embed
            if embed is None:
                return
            agent_id = data.get("agent_id") or "pending"
            summary = data.get("snippet") or data.get("result")
            if data.get("error"):
                state = "error"
                summary = str(data["error"])
            embed = build_starter_embed(
                agent_id=agent_id,
                prompt=prompt,
                project=project.alias if project.alias != "_explicit" else None,
                repo=project.repo,
                status=state,
                run_id=data.get("run_id"),
                summary=summary,
                pr_url=data.get("pr_url"),
                branch=data.get("branch"),
            )
            view = controls_for(
                self.bot,
                agent_id=agent_id,
                status=state,
                pr_url=data.get("pr_url"),
            )
            try:
                await status_message.edit(embed=embed, view=view)
            except discord.HTTPException:
                logger.warning("failed to edit status message", exc_info=True)

        try:
            outcome = await self.bot.cursor.create_and_run(
                prompt=prompt,
                repo_url=project.repo,
                branch=project.branch,
                model=project.model,
                metadata={
                    "source": "discord",
                    "project": project.alias,
                    "thread_id": str(thread.id),
                },
                on_status=on_status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_and_run failed")
            await thread.send(f"Agent failed to start: `{exc}`")
            return

        embed = build_starter_embed(
            agent_id=outcome.agent_id,
            prompt=prompt,
            project=project.alias if project.alias != "_explicit" else None,
            repo=project.repo,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            branch=outcome.branch,
        )
        view = controls_for(
            self.bot,
            agent_id=outcome.agent_id,
            status=outcome.status,
            pr_url=outcome.pr_url,
        )
        try:
            await status_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            await thread.send(embed=embed, view=view)

        bits = [
            f"**{outcome.status.title()}.** Agent `{outcome.agent_id}`.",
            "Reply in this thread to continue.",
        ]
        if outcome.branch:
            bits.append(f"Branch `{outcome.branch}`")
        if outcome.pr_url:
            bits.append(f"PR: {outcome.pr_url}")
        await thread.send(" · ".join(bits))

    async def _follow_up(
        self,
        *,
        interaction: discord.Interaction,
        agent_id: str,
        prompt: str,
        repo: str,
        project_alias: str | None,
    ) -> None:
        status_msg = await interaction.followup.send(
            embed=build_starter_embed(
                agent_id=agent_id,
                prompt=prompt,
                project=project_alias,
                repo=repo,
                status="starting",
            )
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            prompt=prompt,
            repo=repo,
            project_alias=project_alias,
        )

    async def _follow_up_message(
        self,
        *,
        message: discord.Message,
        agent_id: str,
        prompt: str,
        repo: str,
        project_alias: str | None,
    ) -> None:
        status_msg = await message.reply(
            embed=build_starter_embed(
                agent_id=agent_id,
                prompt=prompt,
                project=project_alias,
                repo=repo,
                status="starting",
            ),
            mention_author=False,
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            prompt=prompt,
            repo=repo,
            project_alias=project_alias,
        )

    async def _drive_follow_up(
        self,
        *,
        status_message: discord.Message,
        agent_id: str,
        prompt: str,
        repo: str,
        project_alias: str | None,
    ) -> None:
        embed = status_message.embeds[0] if status_message.embeds else None

        async def on_status(state: str, data: dict) -> None:
            nonlocal embed
            summary = data.get("snippet") or data.get("result") or data.get("error")
            if data.get("error"):
                state = "error"
            embed = build_starter_embed(
                agent_id=agent_id,
                prompt=prompt,
                project=project_alias,
                repo=repo,
                status=state,
                summary=summary,
                run_id=data.get("run_id"),
                pr_url=data.get("pr_url"),
                branch=data.get("branch"),
            )
            view = controls_for(
                self.bot,
                agent_id=agent_id,
                status=state,
                pr_url=data.get("pr_url"),
            )
            try:
                await status_message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

        try:
            outcome = await self.bot.cursor.follow_up(
                agent_id=agent_id, prompt=prompt, on_status=on_status
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("follow_up failed")
            await status_message.reply(
                f"Follow-up failed: `{exc}`", mention_author=False
            )
            return

        embed = build_starter_embed(
            agent_id=outcome.agent_id,
            prompt=prompt,
            project=project_alias,
            repo=repo,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            branch=outcome.branch,
        )
        view = controls_for(
            self.bot,
            agent_id=outcome.agent_id,
            status=outcome.status,
            pr_url=outcome.pr_url,
        )
        try:
            await status_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass


def _strip_mention(content: str, bot_user: discord.ClientUser) -> str:
    text = content
    for form in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
        text = text.replace(form, "")
    return text.strip()


def _is_bot_mention_only(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    return not _strip_mention(message.content, bot_user)


def _thread_name(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if len(cleaned) > 80:
        cleaned = cleaned[:77] + "…"
    return cleaned or "cursor-agent"


async def setup(bot: CursorDiscordBot) -> None:
    await bot.add_cog(AgentCog(bot))
