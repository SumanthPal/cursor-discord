from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.agent_cards import (
    AgentHandoffView,
    completion_embed,
    launch_embed,
)
from bot.agent_ref import (
    AgentRef,
    chat_failed,
    is_agent_id,
    resolve_agent_ref_in_channel,
)
from bot.agent_sessions import agent_dashboard_url, repo_preference_channel_id
from bot.cursor_parse import (
    LaunchOptions,
    MentionKind,
    is_stop_message,
    merge_launch_options,
    parse_mention,
)
from bot.thread_context import augment_prompt_with_thread
from bot.channel_bind import (
    pin_channel_repo,
    resolve_topic_channel,
    topic_from_channel,
)
from bot.chat_ui import ChatStatusEditor, chat_ack, chat_status_body
from bot.discord_text import clip_discord, chunk_discord_lines
from bot.cloud_artifacts import deliver_new_cloud_artifacts
from bot.cloud_steer import steer_ack
from bot.multimodal import AgentPrompt, agent_prompt_from_message, coalesce_ack
from bot.projects import (
    Project,
    compact_repo,
    match_allowed_repo,
    normalize_repo,
    same_repo,
    set_channel_project_topic,
    split_repo_prefix,
)
from bot.repo_refs import decode_repo_ref, encode_repo_ref
from bot.turn_coordinator import (
    AgentBusyError,
    FollowUpJob,
    is_agent_busy_error,
)
from bot.ui import SetupView, short_repo_label

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

AGENTS_DASHBOARD_URL = "https://cursor.com/agents"

HELP_TEXT = """**Cursor** — cloud agents in Discord (Slack-style)
• `@Cursor …` — start or follow up in the agent thread (`repo=`, `branch=`, `model=`, `autopr=false`)
• `@Cursor agent …` — force a **new** agent in this thread
• `@Cursor settings` — show channel repo defaults · `@Cursor settings repos:owner/a,owner/b` to pin
• `@Cursor list my agents` — your recent runs · `@Cursor help` — this message
• In an **agent thread**, reply **without** `@Cursor` to follow up · in the **channel**, `@Cursor` starts a run
• While I'm busy, extra messages **redirect the current run** (API steer) · `stop` cancels
• `/ask` / `/cancel` / `/status` / `/artifacts` also work · PR + demo artifacts when a run finishes

**Admins** (`SUPERUSER_IDS`): `/setup`, `/grant`, `/revoke` · Open runs: {dashboard}
"""

# How long we wait for a plain chat reply with owner/repo after asking.
PENDING_REPO_TTL_SEC = 300

ONBOARDING_TEXT = """**Thanks for adding Cursor.** Setup for admins (`SUPERUSER_IDS`):
1. Create channels for Cursor work and run `/setup` in each to choose which repos it can touch.
2. `/grant @member` gives someone the **{role}** role. They can use me in any set-up channel, on that channel's repos only.
3. `/revoke @member` removes access.

I need **Manage Channels** (repo bindings live in channel topics) and **Manage Roles** with my role above **{role}** (for `/grant`)."""

OPEN_MODE_WARNING = (
    "\n\n**Warning:** `SUPERUSER_IDS` isn't set, so everyone here is treated as an admin."
)

NOT_SET_UP = "This channel isn't set up for Cursor yet. Ask an admin to run `/setup` here."


@dataclass(frozen=True)
class RepoChoice:
    """Need the user to name a repo from ``urls``; ``pin`` binds the channel."""

    urls: list[str]
    pin: bool


@dataclass
class PendingRepoAsk:
    """Waiting for the user to reply with owner/repo or a GitHub URL."""

    agent_prompt: AgentPrompt
    choice: RepoChoice
    owner_id: int
    channel_id: int
    expires_at: float
    thread_anchor_message_id: int | None = None


class AgentCog(commands.Cog):
    def __init__(self, bot: CursorDiscordBot) -> None:
        self.bot = bot
        bot.turns.set_follow_up_handlers(
            start=self._start_queued_follow_up,
            ack=self._ack_queued_follow_up,
        )
        bot.turns.set_steer_handler(
            steer=self._steer_follow_up,
            is_run_active=bot.cursor.is_run_active,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.exception("slash error: %s", error)
        msg = clip_discord(f"Error: {error}")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="help", description="How to use Cursor")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            HELP_TEXT.format(
                role=self.bot.settings.cursor_role_name,
                dashboard=AGENTS_DASHBOARD_URL,
            ),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            logger.info("joined %s (%s); no system channel to onboard", guild, guild.id)
            return
        text = ONBOARDING_TEXT.format(role=self.bot.settings.cursor_role_name)
        if self.bot.policy.open_mode:
            text += OPEN_MODE_WARNING
        await channel.send(text)

    @app_commands.command(
        name="setup", description="Admin: choose which repos this channel can use"
    )
    @app_commands.describe(
        repos="Comma-separated owner/repo list; omit to pick from a menu",
        clear="Remove this channel's repo binding",
    )
    @app_commands.guild_only()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        repos: str | None = None,
        clear: bool = False,
    ) -> None:
        denied = self.bot.policy.check_guild(
            interaction.guild
        ) or self.bot.policy.check_admin(interaction.user)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        if clear or repos:
            urls = [normalize_repo(r) for r in (repos or "").split(",") if r.strip()]
            note = await pin_channel_repo(
                self.bot, interaction.channel, [] if clear else urls, force=True
            )
            await interaction.followup.send(note, ephemeral=True)
            return

        try:
            urls = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Could not load Cursor repos: {exc}", ephemeral=True
            )
            return
        if not urls:
            await interaction.followup.send(
                "No Cursor-connected repos. Link GitHub in the Cursor dashboard "
                "for this API key, or pass `repos:` explicitly.",
                ephemeral=True,
            )
            return
        note = (
            "Pick repos below, or re-run with text — "
            "`/setup repos:owner/a,owner/b` (preferred when you have many)."
        )
        if len(urls) > 25:
            note = (
                f"Menu shows 25 of {len(urls)}. Prefer "
                "`/setup repos:owner/a,owner/b` for the full set."
            )
        await interaction.followup.send(
            note,
            view=SetupView(
                self.bot,
                repo_urls=urls,
                owner_id=interaction.user.id,
                channel=interaction.channel,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="grant", description="Admin: give a member Cursor access")
    @app_commands.guild_only()
    async def grant_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await self._set_access(interaction, member, grant=True)

    @app_commands.command(name="revoke", description="Admin: remove a member's Cursor access")
    @app_commands.guild_only()
    async def revoke_cmd(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await self._set_access(interaction, member, grant=False)

    async def _set_access(
        self, interaction: discord.Interaction, member: discord.Member, *, grant: bool
    ) -> None:
        guild = interaction.guild
        denied = self.bot.policy.check_guild(guild) or self.bot.policy.check_admin(
            interaction.user
        )
        if denied or guild is None:
            await interaction.response.send_message(
                denied or "Server only.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        name = self.bot.settings.cursor_role_name
        # The bot's own integration role usually shares this name and can't be assigned.
        role = next(
            (r for r in guild.roles if r.name == name and not r.managed), None
        )
        if not grant and (role is None or role not in member.roles):
            msg = f"{member.mention} doesn't have the **{name}** role."
            if member.id in self.bot.settings.allowed_user_ids:
                msg += " They still have access via `ALLOWED_USER_IDS`."
            elif self.bot.policy.is_admin(member):
                msg += " They still have access as an admin via `SUPERUSER_IDS`."
            await interaction.followup.send(msg, ephemeral=True)
            return
        try:
            if grant:
                if role is None:
                    role = await guild.create_role(
                        name=name, reason="Cursor bot access role"
                    )
                await member.add_roles(role, reason=f"/grant by {interaction.user}")
            else:
                await member.remove_roles(role, reason=f"/revoke by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                f"I need **Manage Roles**, and my role must be above **{name}**.",
                ephemeral=True,
            )
            return
        if grant:
            msg = f"{member.mention} can now use Cursor in channels set up with `/setup`."
        else:
            msg = f"Removed Cursor access from {member.mention}."
        if not grant and member.id in self.bot.settings.allowed_user_ids:
            msg += " (They still have access via `ALLOWED_USER_IDS`.)"
        elif self.bot.policy.is_admin(member) and not grant:
            msg += " (They're still an admin via `SUPERUSER_IDS`.)"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name="artifacts",
        description="Post new cloud agent demo artifacts into this thread",
    )
    @app_commands.describe(
        agent_id="Optional bc-… id; defaults to the agent for this thread",
    )
    async def artifacts_cmd(
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

        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "Run `/artifacts` inside an agent thread.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        resolved_id = agent_id.strip() if agent_id else None
        if not resolved_id:
            assert self.bot.user is not None
            ref = await resolve_agent_ref_in_channel(
                channel,
                bot_user_id=self.bot.user.id,
                thread_agents=self.bot.thread_agents,
            )
            if not ref:
                await interaction.followup.send(
                    "No agent in this thread. Pass `agent_id` or start with `/ask`.",
                    ephemeral=True,
                )
                return
            resolved_id = ref.agent_id

        if not is_agent_id(resolved_id):
            await interaction.followup.send(
                "agent_id must look like `bc-…`", ephemeral=True
            )
            return

        posted = self.bot.posted_artifacts.setdefault(resolved_id, set())
        count = await deliver_new_cloud_artifacts(
            thread=channel,
            cursor=self.bot.cursor,
            agent_id=resolved_id,
            posted=posted,
        )
        if count:
            await interaction.followup.send(
                f"Posted {count} artifact(s) to the thread.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "No new artifacts to post (already shared or none on the agent yet).",
                ephemeral=True,
            )

    @app_commands.command(name="ask", description="Start a Cursor cloud agent")
    @app_commands.describe(
        prompt="What to do",
        repo="Repo override (autocomplete); else channel default or picker",
    )
    async def ask_cmd(
        self,
        interaction: discord.Interaction,
        prompt: str,
        repo: str | None = None,
    ) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        resolved_repo = None
        if repo:
            try:
                resolved_repo = decode_repo_ref(repo, self.bot.repo_ref_cache)
            except ValueError:
                resolved_repo = repo
        # Defer before channel fetch / list_repos — can exceed Discord's 3s window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = await self._plan_launch(
            user=interaction.user, channel=interaction.channel, repo=resolved_repo
        )
        if isinstance(plan, str):
            await interaction.followup.send(plan, ephemeral=True)
            return
        if isinstance(plan, RepoChoice):
            # Slash can't take a free-text reply on the same interaction — ask in-channel.
            await self._ask_repo_in_channel(
                channel=interaction.channel,
                user=interaction.user,
                agent_prompt=AgentPrompt.from_text(prompt),
                choice=plan,
            )
            await interaction.followup.send(
                "Reply in the channel with `owner/repo` or a GitHub URL "
                "(or re-run `/ask` with the `repo:` option).",
                ephemeral=True,
            )
            return

        if interaction.channel is not None:
            self._clear_pending_repo(interaction.channel.id, interaction.user.id)

        await self._launch_new_agent(
            interaction=interaction,
            agent_prompt=AgentPrompt.from_text(prompt),
            project=plan,
            announce_channel=interaction.channel,
        )

    @ask_cmd.autocomplete("repo")
    async def ask_repo_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            urls = await self._visible_repos(interaction.user, interaction.channel)
        except Exception:  # noqa: BLE001
            logger.exception("repo autocomplete failed")
            return []
        current_l = (current or "").lower()
        choices: list[app_commands.Choice[str]] = []
        for url in urls:
            label = short_repo_label(url)
            if (
                current_l
                and current_l not in label.lower()
                and current_l not in url.lower()
            ):
                continue
            value = encode_repo_ref(url, self.bot.repo_ref_cache)
            choices.append(
                app_commands.Choice(name=label[:100], value=value[:100])
            )
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(
        name="repos",
        description="List repos you can use here",
    )
    async def repos_cmd(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild, consume_rate=False
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        admin = self.bot.policy.is_admin(interaction.user)
        try:
            urls = await self._visible_repos(interaction.user, interaction.channel)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Could not list repos (is CURSOR_API_KEY valid / GitHub linked?): {exc}",
                ephemeral=True,
            )
            return
        if not urls:
            await interaction.followup.send(
                "No repos returned. Connect GitHub in the Cursor dashboard "
                "for the account that owns this API key."
                if admin
                else NOT_SET_UP,
                ephemeral=True,
            )
            return
        # Compact labels only — full URLs blow past Discord's 2000-char content cap.
        lines = [f"• `{short_repo_label(u)}`" for u in urls]
        header = (
            f"**{len(urls)} Cursor-connected repo(s)**"
            if admin
            else f"**{len(urls)} repo(s) available in this channel**"
        )
        pages = chunk_discord_lines(
            header,
            lines,
            footer="`@Cursor …` or `/ask` to start.",
        )
        for page in pages:
            await interaction.followup.send(page, ephemeral=True)

    @app_commands.command(
        name="talk",
        description="Follow-up by agent id (prefer thread reply)",
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
            agent_prompt=AgentPrompt.from_text(prompt),
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
            ref = await resolve_agent_ref_in_channel(
                interaction.channel,
                bot_user_id=self.bot.user.id,
                thread_agents=self.bot.thread_agents,
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

        summary = str(info.get("summary") or "—")
        lines = [
            f"**agent_id:** `{info['agent_id']}`",
            f"**status:** {info.get('status') or 'unknown'}",
            f"**name:** {info.get('name') or '—'}",
            f"**summary:** {summary}",
        ]
        if info.get("repos"):
            labels = [short_repo_label(r) for r in info["repos"]]
            lines.append("**repos:** " + ", ".join(f"`{x}`" for x in labels))
        if info.get("active_run_id"):
            lines.append(f"**active_run:** `{info['active_run_id']}`")
        await interaction.followup.send(
            clip_discord("\n".join(lines)), ephemeral=True
        )

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
            ref = await resolve_agent_ref_in_channel(
                interaction.channel,
                bot_user_id=self.bot.user.id,
                thread_agents=self.bot.thread_agents,
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
                "No nicknames. Use `@Cursor` / `/ask` — first pick pins the channel.",
                ephemeral=True,
            )
            return
        default = self.bot.registry.default
        lines = []
        for alias in aliases:
            p = self.bot.registry.get(alias)
            assert p is not None
            mark = " (default)" if alias == default else ""
            ref = p.branch or "default"
            lines.append(f"- **{alias}**{mark}: `{p.repo}` @ `{ref}`")
        pages = chunk_discord_lines("**Project nicknames**", lines)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

    @project.command(
        name="bind",
        description="Bind this channel to a project alias via channel topic",
    )
    @app_commands.describe(alias="Project alias from the registry")
    async def project_bind(
        self, interaction: discord.Interaction, alias: str
    ) -> None:
        denied = self.bot.policy.check_guild(
            interaction.guild
        ) or self.bot.policy.check_admin(interaction.user)
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
        channel = await resolve_topic_channel(self.bot, interaction.channel)
        if channel is None:
            await interaction.response.send_message(
                "Bind only works on text/forum channels.", ephemeral=True
            )
            return
        me = channel.guild.me
        if me is None or not channel.permissions_for(me).manage_channels:
            await interaction.response.send_message(
                "I need **Manage Channels** on this channel "
                "(check channel permission overwrites).",
                ephemeral=True,
            )
            return
        new_topic = set_channel_project_topic(channel.topic, alias_l)
        await channel.edit(topic=new_topic)
        await interaction.response.send_message(
            f"Bound #{channel.name} → `{alias_l}` "
            f"(topic marker `cursor-project:{alias_l}`).",
            ephemeral=True,
        )

    async def _handle_stop_in_thread(
        self, message: discord.Message, *, agent_id: str, run_id: str | None
    ) -> None:
        self.bot.turns.clear_pending(agent_id)
        try:
            msg = await self.bot.cursor.cancel(agent_id, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            await message.reply(f"Stop failed: `{exc}`", mention_author=False)
            return
        await message.reply(msg, mention_author=False)

    async def _reply_help(self, message: discord.Message) -> None:
        text = HELP_TEXT.format(
            role=self.bot.settings.cursor_role_name,
            dashboard=AGENTS_DASHBOARD_URL,
        )
        await message.reply(clip_discord(text), mention_author=False)

    async def _reply_settings(
        self, message: discord.Message, repos: tuple[str, ...]
    ) -> None:
        denied = self.bot.policy.authorize(
            user=message.author, guild=message.guild, consume_rate=False
        )
        if denied:
            await message.reply(denied, mention_author=False)
            return
        if repos:
            denied_admin = self.bot.policy.check_admin(message.author)
            if denied_admin:
                await message.reply(denied_admin, mention_author=False)
                return
            note = await pin_channel_repo(
                self.bot, message.channel, list(repos), force=True
            )
            await message.reply(note, mention_author=False)
            return
        bound = await self._channel_projects(message.channel)
        if bound:
            labels = ", ".join(f"`{compact_repo(p.repo)}`" for p in bound)
            await message.reply(
                f"**Channel repos:** {labels}\n"
                "Change with `@Cursor settings repos:owner/a,owner/b` "
                "(needs Manage Channels).",
                mention_author=False,
            )
            return
        await message.reply(
            "No channel default yet. "
            "`@Cursor settings repos:owner/repo` or ask an admin to `/setup`.",
            mention_author=False,
        )

    async def _reply_list_agents(self, message: discord.Message) -> None:
        denied = self.bot.policy.authorize(
            user=message.author, guild=message.guild, consume_rate=False
        )
        if denied:
            await message.reply(denied, mention_author=False)
            return
        items = self.bot.sessions.list_for_user(message.author.id)
        if not items:
            await message.reply(
                "No agents from you in this session yet. Say `@Cursor …` to start one.",
                mention_author=False,
            )
            return
        lines = []
        for s in items:
            lines.append(
                f"• `{s.agent_id}` · `{compact_repo(s.repo)}` · {s.status} · "
                f"{agent_dashboard_url(s.agent_id)}"
            )
        await message.reply(
            clip_discord("**Your recent agents**\n" + "\n".join(lines)),
            mention_author=False,
        )

    async def _handle_agent_thread_message(
        self, message: discord.Message, ref: AgentRef
    ) -> bool:
        """Follow-up in a thread (guild or DM) with an existing agent. Returns True if consumed."""
        if self._get_pending_repo(message.channel.id, message.author.id) is not None:
            return True

        mentioned = self.bot.user in message.mentions
        prompt_text = (
            _strip_mention(message.content, self.bot.user)
            if mentioned
            else message.content.strip()
        )

        if mentioned:
            parsed = parse_mention(prompt_text)
            if parsed.kind == MentionKind.HELP:
                await self._reply_help(message)
                return True
            if parsed.kind == MentionKind.SETTINGS:
                await self._reply_settings(message, parsed.settings_repos)
                return True
            if parsed.kind == MentionKind.LIST_AGENTS:
                await self._reply_list_agents(message)
                return True
            if parsed.kind == MentionKind.STOP:
                denied = self.bot.policy.authorize(
                    user=message.author, guild=message.guild
                )
                if denied:
                    await message.reply(denied, mention_author=False)
                    return True
                await self._handle_stop_in_thread(
                    message, agent_id=ref.agent_id, run_id=ref.run_id
                )
                return True
            if parsed.kind == MentionKind.FORCE_AGENT:
                return False
            elif parsed.kind == MentionKind.PROMPT:
                prompt_text = (
                    _strip_mention(message.content, self.bot.user)
                    if mentioned
                    else message.content.strip()
                )
            elif _is_bot_mention_only(message, self.bot.user):
                await self._reply_help(message)
                return True

        if not (prompt_text or message.attachments) or prompt_text.startswith("/"):
            return mentioned

        denied = self.bot.policy.authorize(
            user=message.author, guild=message.guild
        )
        if denied:
            await message.reply(denied, mention_author=False)
            return True

        agent_prompt = await agent_prompt_from_message(message, text=prompt_text)
        if not agent_prompt.has_content:
            if agent_prompt.feedback:
                await message.reply(
                    clip_discord(agent_prompt.feedback), mention_author=False
                )
            return True
        await self._follow_up_message(
            message=message,
            agent_id=ref.agent_id,
            agent_prompt=agent_prompt,
            repo=ref.repo or "(existing agent)",
            project_alias=ref.project,
        )
        return True

    async def _launch_from_switch(
        self,
        *,
        interaction: discord.Interaction,
        repo: str,
        prompt: AgentPrompt,
        channel_id: int,
    ) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        project = Project(
            alias="_switch",
            repo=normalize_repo(repo),
            model=self.bot.settings.cursor_model,
        )
        thread, status_message = await self._open_agent_thread(
            channel=channel,
            prompt=prompt.display,
            repo=project.repo,
            thread_anchor=None,
            interaction_user=interaction.user,
        )
        await self._run_create(
            status_message=status_message,
            thread=thread,
            agent_prompt=prompt,
            project=project,
            discord_user=interaction.user,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.bot.user is None:
            return
        _alias_role_mentions_to_bot(
            message, self.bot.user, self.bot.settings.cursor_role_name
        )

        # Plain reply naming a repo after we asked "which repo?"
        if await self._maybe_consume_repo_reply(message):
            return

        ref = None
        if isinstance(message.channel, (discord.Thread, discord.DMChannel)):
            ref = await resolve_agent_ref_in_channel(
                message.channel,
                bot_user_id=self.bot.user.id,
                thread_agents=self.bot.thread_agents,
            )

        if ref and is_stop_message(message.content, bot_user_id=self.bot.user.id):
            denied = self.bot.policy.authorize(
                user=message.author, guild=message.guild
            )
            if denied:
                await message.reply(denied, mention_author=False)
                return
            await self._handle_stop_in_thread(
                message, agent_id=ref.agent_id, run_id=ref.run_id
            )
            return

        if isinstance(message.channel, (discord.Thread, discord.DMChannel)) and ref:
            if (
                isinstance(message.channel, discord.Thread)
                and self._get_pending_repo(message.channel.id, message.author.id)
                is not None
            ):
                await message.reply(
                    "Still waiting — reply with `owner/repo` for that question, "
                    "or `@Cursor …` in the channel for a new request.",
                    mention_author=False,
                )
                return
            if await self._handle_agent_thread_message(message, ref):
                return

        if isinstance(message.channel, discord.DMChannel):
            if ref and self.bot.user in message.mentions:
                tail = _strip_mention(message.content, self.bot.user)
                if parse_mention(tail).kind != MentionKind.FORCE_AGENT:
                    return
            elif ref:
                return
            await message.reply(
                "Start a cloud agent from a server channel with `@Cursor …`. "
                "In this DM, reply **without** `@` to follow up once a run exists.",
                mention_author=False,
            )
            return

        if self.bot.user not in message.mentions:
            return
        prompt_text = _strip_mention(message.content, self.bot.user)
        parsed = parse_mention(prompt_text)

        if parsed.kind == MentionKind.HELP:
            await self._reply_help(message)
            return
        if parsed.kind == MentionKind.SETTINGS:
            await self._reply_settings(message, parsed.settings_repos)
            return
        if parsed.kind == MentionKind.LIST_AGENTS:
            await self._reply_list_agents(message)
            return
        if parsed.kind == MentionKind.STOP and ref:
            denied = self.bot.policy.authorize(
                user=message.author, guild=message.guild
            )
            if denied:
                await message.reply(denied, mention_author=False)
                return
            await self._handle_stop_in_thread(
                message, agent_id=ref.agent_id, run_id=ref.run_id
            )
            return

        prompt_body = parsed.prompt if parsed.kind in {
            MentionKind.PROMPT,
            MentionKind.FORCE_AGENT,
        } else prompt_text

        inline_repo = parsed.options.repo
        agent_prompt = await agent_prompt_from_message(message, text=prompt_body)
        if not agent_prompt.has_content and not inline_repo:
            if agent_prompt.feedback:
                await message.reply(
                    clip_discord(agent_prompt.feedback),
                    mention_author=False,
                )
            else:
                await message.reply(
                    "`@Cursor <what to do>` or `@Cursor owner/repo <what to do>`",
                    mention_author=False,
                )
            return
        if not agent_prompt.has_content and inline_repo:
            await message.reply(
                f"Got `{compact_repo(inline_repo)}` — add what you want done "
                f"(e.g. `@Cursor {compact_repo(inline_repo)} fix the flaky test`).",
                mention_author=False,
            )
            return

        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            await message.reply(denied, mention_author=False)
            return

        # Fresh @Cursor cancels any outstanding "which repo?" wait.
        self._clear_pending_repo(message.channel.id, message.author.id)

        plan = await self._plan_launch(
            user=message.author,
            channel=message.channel,
            repo=inline_repo,
            options=parsed.options,
        )
        if isinstance(plan, str):
            await message.reply(plan, mention_author=False)
            return
        if isinstance(plan, RepoChoice):
            await self._ask_repo_in_channel(
                channel=message.channel,
                user=message.author,
                agent_prompt=agent_prompt,
                choice=plan,
                thread_anchor_message_id=message.id,
            )
            return

        project, auto_pr = self._apply_launch_options(plan, parsed.options)

        # Admin named a repo in an unbound channel — pin it like the old first-pick.
        if (
            inline_repo
            and self.bot.policy.is_admin(message.author)
            and not await self._channel_projects(message.channel)
        ):
            pin_note = await pin_channel_repo(
                self.bot, message.channel, [project.repo]
            )
            await message.reply(pin_note, mention_author=False)

        await self._launch_new_agent_from_message(
            message=message,
            agent_prompt=agent_prompt,
            project=project,
            auto_create_pr=auto_pr,
        )

    async def _channel_projects(
        self, channel: discord.abc.Messageable | None
    ) -> list[Project]:
        topic_channel = await resolve_topic_channel(self.bot, channel)
        return self.bot.registry.channel_projects(
            topic_from_channel(topic_channel), self.bot.settings.cursor_model
        )

    async def _visible_repos(
        self, user: discord.abc.User, channel: discord.abc.Messageable | None
    ) -> list[str]:
        """Admins see every repo on the API key; members only this channel's."""
        if self.bot.policy.is_admin(user):
            return await self.bot.cursor.list_repos()
        return [p.repo for p in await self._channel_projects(channel)]

    def _apply_launch_options(
        self, project: Project, options: LaunchOptions
    ) -> tuple[Project, bool]:
        branch, model = merge_launch_options(
            options,
            project.branch,
            project.model or self.bot.settings.cursor_model,
        )
        updated = Project(
            alias=project.alias,
            repo=project.repo,
            branch=branch,
            model=model,
        )
        auto_pr = (
            True if options.auto_create_pr is None else options.auto_create_pr
        )
        return updated, auto_pr

    def _repo_preference_channel_id(
        self, channel: discord.abc.Messageable | None
    ) -> int | None:
        if channel is None:
            return None
        cid = getattr(channel, "id", None)
        if not isinstance(cid, int):
            return None
        parent_id = getattr(channel, "parent_id", None)
        if not isinstance(parent_id, int):
            parent_id = None
        return repo_preference_channel_id(cid, parent_id=parent_id)

    async def _recent_repo_for_channel(
        self, channel: discord.abc.Messageable | None, user_id: int
    ) -> str | None:
        pref = self._repo_preference_channel_id(channel)
        if pref is None:
            return None
        return self.bot.sessions.recent_repo(pref, user_id)

    async def _plan_launch(
        self,
        *,
        user: discord.abc.User,
        channel: discord.abc.Messageable | None,
        repo: str | None,
        options: LaunchOptions | None = None,
    ) -> Project | RepoChoice | str:
        """Project to launch, a picker to show, or a user-facing refusal."""
        opts = options or LaunchOptions()
        bound = await self._channel_projects(channel)
        admin = self.bot.policy.is_admin(user)
        model = self.bot.settings.cursor_model

        repo = repo or opts.repo

        if repo:
            allowed_urls = [p.repo for p in bound]
            if admin and not allowed_urls:
                try:
                    allowed_urls = await self.bot.cursor.list_repos()
                except Exception as exc:  # noqa: BLE001
                    return f"Could not load Cursor repos: {exc}"
            matched = match_allowed_repo(repo, allowed_urls) if allowed_urls else None
            if matched:
                return Project(alias="_explicit", repo=matched, model=model)
            if admin and not bound:
                # Admin typed a repo that isn't on the API key — still try exact URL.
                return Project(
                    alias="_explicit", repo=normalize_repo(repo), model=model
                )
            if not bound:
                return NOT_SET_UP
            allowed = ", ".join(f"`{compact_repo(p.repo)}`" for p in bound)
            return (
                f"`{compact_repo(repo)}` isn't allowed in this channel. "
                f"Allowed: {allowed}"
            )

        if len(bound) == 1:
            return bound[0]
        if bound:
            recent = await self._recent_repo_for_channel(channel, user.id)
            if recent:
                matched = match_allowed_repo(recent, [p.repo for p in bound])
                if matched:
                    return Project(
                        alias="_recent",
                        repo=matched,
                        branch=opts.branch,
                        model=opts.model or model,
                    )
            return RepoChoice(urls=[p.repo for p in bound], pin=False)
        if not admin:
            return NOT_SET_UP

        fallback = self.bot.registry.resolve(default_model=model)
        if fallback:
            return fallback
        try:
            urls = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            return f"Could not load Cursor repos: {exc}"
        if not urls:
            return (
                "No Cursor-connected repos. Link GitHub in the Cursor dashboard "
                "for this API key, or pass `repo:` explicitly."
            )
        return RepoChoice(urls=urls, pin=True)

    def _pending_key(
        self, channel_id: int, user_id: int
    ) -> tuple[int, int]:
        return (channel_id, user_id)

    def _clear_pending_repo(self, channel_id: int, user_id: int) -> None:
        self.bot.pending_repo_asks.pop(
            self._pending_key(channel_id, user_id), None
        )

    def _get_pending_repo(
        self, channel_id: int, user_id: int
    ) -> PendingRepoAsk | None:
        key = self._pending_key(channel_id, user_id)
        pending = self.bot.pending_repo_asks.get(key)
        if not isinstance(pending, PendingRepoAsk):
            return None
        if time.monotonic() > pending.expires_at:
            self.bot.pending_repo_asks.pop(key, None)
            return None
        return pending

    async def _ask_repo_in_channel(
        self,
        *,
        channel: discord.abc.Messageable | None,
        user: discord.abc.User,
        agent_prompt: AgentPrompt,
        choice: RepoChoice,
        thread_anchor_message_id: int | None = None,
    ) -> None:
        if channel is None or not hasattr(channel, "id"):
            return
        channel_id = int(channel.id)
        labels = [compact_repo(u) for u in choice.urls]
        lines = [
            f"Which repo for: **{_truncate_prompt(agent_prompt.display)}**?",
            "Reply with `owner/repo` or a GitHub URL.",
        ]
        if len(labels) <= 12:
            lines.append("Allowed: " + ", ".join(f"`{x}`" for x in labels))
        else:
            lines.append(
                f"{len(labels)} repos available — `/repos` to list, or type a name fragment."
            )
        posted = await channel.send("\n".join(lines))
        anchor_id = thread_anchor_message_id or getattr(posted, "id", None)
        self.bot.pending_repo_asks[self._pending_key(channel_id, user.id)] = (
            PendingRepoAsk(
                agent_prompt=agent_prompt,
                choice=choice,
                owner_id=user.id,
                channel_id=channel_id,
                expires_at=time.monotonic() + PENDING_REPO_TTL_SEC,
                thread_anchor_message_id=anchor_id,
            )
        )

    async def _maybe_consume_repo_reply(self, message: discord.Message) -> bool:
        """If this message answers a pending repo ask, launch (or explain) and return True."""
        if self.bot.user in message.mentions:
            return False  # handled as a fresh @Cursor below
        pending = self._get_pending_repo(message.channel.id, message.author.id)
        if pending is None:
            return False

        typed = (message.content or "").strip()
        if not typed or typed.startswith("/"):
            return False

        # Whole message should be a repo ref (optional leading peel).
        peeled, rest = split_repo_prefix(typed)
        if peeled and rest:
            # Looks like "owner/repo do stuff" — treat as cancel + they should @Cursor.
            return False
        candidate = peeled or typed.strip().strip("`")
        matched = match_allowed_repo(candidate, pending.choice.urls)
        if matched is None:
            # Only claim the message if it looks like they tried to name a repo.
            if peeled or ("/" in candidate and " " not in candidate):
                allowed = ", ".join(
                    f"`{compact_repo(u)}`" for u in pending.choice.urls[:12]
                )
                extra = "" if len(pending.choice.urls) <= 12 else " …"
                await message.reply(
                    f"`{compact_repo(candidate)}` isn't available here. "
                    f"Try one of: {allowed}{extra}",
                    mention_author=False,
                )
                return True
            return False

        self._clear_pending_repo(message.channel.id, message.author.id)
        denied = self.bot.policy.authorize(
            user=message.author, guild=message.guild
        )
        if denied:
            await message.reply(denied, mention_author=False)
            return True

        if pending.choice.pin:
            pin_note = await pin_channel_repo(
                self.bot, message.channel, [matched]
            )
            await message.reply(pin_note, mention_author=False)

        project = Project(
            alias="_explicit",
            repo=matched,
            model=self.bot.settings.cursor_model,
        )
        thread_anchor: discord.Message | None = None
        if pending.thread_anchor_message_id:
            try:
                thread_anchor = await message.channel.fetch_message(
                    pending.thread_anchor_message_id
                )
            except discord.NotFound:
                logger.warning(
                    "repo reply anchor missing (id=%s)",
                    pending.thread_anchor_message_id,
                )
        await self._launch_new_agent_from_message(
            message=message,
            agent_prompt=pending.agent_prompt,
            project=project,
            thread_anchor=thread_anchor,
        )
        return True

    async def _launch_new_agent(
        self,
        *,
        interaction: discord.Interaction,
        agent_prompt: AgentPrompt,
        project: Project,
        announce_channel: discord.abc.Messageable | None,
    ) -> None:
        channel = announce_channel or interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Need a text channel or thread.")
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        thread, status_message = await self._open_agent_thread(
            channel=channel,
            prompt=agent_prompt.display,
            repo=project.repo,
            thread_anchor=None,
            interaction_user=interaction.user,
        )
        await _clear_deferred_thinking(interaction, status_message)
        await self._run_create(
            status_message=status_message,
            thread=thread,
            agent_prompt=agent_prompt,
            project=project,
            discord_user=interaction.user,
        )

    async def _launch_new_agent_from_message(
        self,
        *,
        message: discord.Message,
        agent_prompt: AgentPrompt,
        project: Project,
        thread_anchor: discord.Message | None = None,
        auto_create_pr: bool = True,
    ) -> None:
        channel = message.channel
        if not isinstance(
            channel, (discord.TextChannel, discord.Thread, discord.DMChannel)
        ):
            await message.reply("Need a text channel.", mention_author=False)
            return
        thread, status_message = await self._open_agent_thread(
            channel=channel,
            prompt=agent_prompt.display,
            repo=project.repo,
            thread_anchor=thread_anchor or message,
        )
        await self._run_create(
            status_message=status_message,
            thread=thread,
            agent_prompt=agent_prompt,
            project=project,
            auto_create_pr=auto_create_pr,
            discord_user=message.author,
        )

    async def _open_agent_thread(
        self,
        *,
        channel: discord.TextChannel | discord.Thread | discord.DMChannel,
        prompt: str,
        repo: str | None,
        thread_anchor: discord.Message | None = None,
        interaction_user: discord.User | discord.Member | None = None,
    ) -> tuple[discord.Thread | discord.DMChannel, discord.Message]:
        """Branch a public thread from the anchor message; first bot reply lives inside the thread."""
        if isinstance(channel, discord.DMChannel):
            anchor = thread_anchor or await channel.send(
                clip_discord(f"**{interaction_user.display_name if interaction_user else 'You'}:** {prompt}")
            )
            status_message = await anchor.reply(
                chat_ack(prompt=prompt, repo=repo, echo_prompt=False),
                mention_author=False,
            )
            return channel, status_message
        if isinstance(channel, discord.Thread):
            if interaction_user is not None:
                await channel.send(
                    clip_discord(f"**{interaction_user.display_name}:** {prompt}")
                )
            status_message = await channel.send(
                chat_ack(
                    prompt=prompt,
                    repo=repo,
                    echo_prompt=interaction_user is None,
                )
            )
            return channel, status_message

        anchor = thread_anchor
        if anchor is None:
            who = (
                interaction_user.display_name
                if interaction_user is not None
                else "Someone"
            )
            anchor = await channel.send(
                clip_discord(f"**{who}:** {prompt}"),
            )

        thread = await self._ensure_thread(channel, anchor, prompt)
        status_message = await thread.send(
            chat_ack(prompt=prompt, repo=repo, echo_prompt=False)
        )
        return thread, status_message

    async def _ensure_thread(
        self,
        channel: discord.TextChannel | discord.Thread,
        anchor: discord.Message,
        prompt: str,
    ) -> discord.Thread:
        if isinstance(channel, discord.Thread):
            return channel
        existing = getattr(anchor, "thread", None)
        if existing is not None:
            return existing
        name = _thread_name(prompt)
        try:
            return await anchor.create_thread(name=name)
        except discord.HTTPException:
            return await channel.create_thread(
                name=name, message=anchor, type=discord.ChannelType.public_thread
            )

    async def _run_create(
        self,
        *,
        status_message: discord.Message,
        thread: discord.Thread | discord.DMChannel,
        agent_prompt: AgentPrompt,
        project: Project,
        auto_create_pr: bool = True,
        discord_user: discord.User | discord.Member | None = None,
    ) -> None:
        editor = ChatStatusEditor(status_message)
        card = launch_embed(
            repo=project.repo,
            branch=project.branch,
            model=project.model,
            prompt=agent_prompt.display,
        )
        await editor.set_content(
            chat_ack(prompt=agent_prompt.display, repo=project.repo),
            embed=card,
            force=True,
        )

        user = discord_user or getattr(status_message, "author", None)
        visible_repos: list[str] = []
        if user is not None:
            try:
                visible_repos = await self._visible_repos(user, thread)
            except Exception:  # noqa: BLE001
                logger.debug("visible repos for handoff failed", exc_info=True)

        async def on_status(state: str, data: dict) -> None:
            agent_id = data.get("agent_id") or "pending"
            if agent_id != "pending" and user is not None:
                parent_id = (
                    thread.parent_id
                    if isinstance(thread, discord.Thread)
                    and isinstance(thread.parent_id, int)
                    else None
                )
                pref_channel = (
                    repo_preference_channel_id(thread.id, parent_id=parent_id)
                    if isinstance(thread, discord.Thread)
                    else thread.id
                )
                self.bot.sessions.remember(
                    agent_id=agent_id,
                    discord_user_id=user.id,
                    channel_id=thread.id,
                    repo=project.repo,
                    prompt_summary=_truncate_prompt(agent_prompt.display),
                    repo_preference_channel_id=pref_channel,
                )
                self.bot.sessions.set_status(agent_id, state)
            summary = data.get("snippet") or data.get("result")
            err = data.get("error")
            if err:
                state = "error"
                summary = str(err)
            body = chat_status_body(
                agent_id=agent_id,
                status=state,
                repo=project.repo,
                snippet=data.get("snippet"),
                summary=summary,
                run_id=data.get("run_id"),
                pr_url=data.get("pr_url"),
                error=str(err) if err else None,
            )
            embed = card if state in {"created", "starting"} else None
            if is_agent_id(agent_id):
                self._bind_thread_agent(
                    thread,
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    repo=project.repo,
                    project=project.alias,
                )
            await editor.set_content(
                body,
                embed=embed,
                force=state not in {"running", "streaming"},
            )
        try:
            await _set_status_reaction(status_message, "running")
            outcome = await self.bot.cursor.create_and_run(
                prompt=agent_prompt.sdk_message(),
                repo_url=project.repo,
                branch=project.branch,
                model=project.model,
                auto_create_pr=auto_create_pr,
                metadata={
                    "source": "discord",
                    "project": project.alias,
                    "thread_id": str(thread.id),
                    "discord_user_id": str(user.id) if user else "",
                },
                on_status=on_status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_and_run failed")
            await thread.send(chat_failed(exc))
            return

        self.bot.sessions.set_status(outcome.agent_id, outcome.status)
        final = chat_status_body(
            agent_id=outcome.agent_id,
            status=outcome.status,
            repo=project.repo,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            error=outcome.error_message,
        )
        done_embed = completion_embed(
            agent_id=outcome.agent_id,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            pr_url=outcome.pr_url,
        )
        view = AgentHandoffView(
            agent_id=outcome.agent_id,
            pr_url=outcome.pr_url,
            repo_urls=visible_repos,
            bot=self.bot,
            relaunch_prompt=agent_prompt.text,
            relaunch_user_id=user.id if user else None,
            relaunch_channel_id=thread.id,
        )
        await editor.set_content(final, embed=done_embed, view=view, force=True)
        await _set_status_reaction(status_message, outcome.status)
        self._bind_thread_agent(
            thread,
            agent_id=outcome.agent_id,
            run_id=outcome.run_id,
            repo=project.repo,
            project=project.alias,
        )
        await self._deliver_artifacts(thread, outcome.agent_id)

    def _bind_thread_agent(
        self,
        thread: discord.Thread | discord.DMChannel,
        *,
        agent_id: str,
        run_id: str | None = None,
        repo: str | None = None,
        project: str | None = None,
    ) -> None:
        if not is_agent_id(agent_id):
            return
        prev = self.bot.thread_agents.get(thread.id)
        self.bot.thread_agents[thread.id] = AgentRef(
            agent_id=agent_id,
            run_id=run_id if run_id is not None else (prev.run_id if prev else None),
            repo=repo or (prev.repo if prev else None),
            project=project or (prev.project if prev else None),
        )

    async def _deliver_artifacts(
        self, thread: discord.abc.Messageable, agent_id: str
    ) -> None:
        if not is_agent_id(agent_id):
            return
        posted = self.bot.posted_artifacts.setdefault(agent_id, set())
        try:
            await deliver_new_cloud_artifacts(
                thread=thread,
                cursor=self.bot.cursor,
                agent_id=agent_id,
                posted=posted,
            )
        except Exception:  # noqa: BLE001
            logger.exception("artifact delivery failed for %s", agent_id)

    async def _follow_up(
        self,
        *,
        interaction: discord.Interaction,
        agent_id: str,
        agent_prompt: AgentPrompt,
        repo: str,
        project_alias: str | None,
    ) -> None:
        channel = interaction.channel
        status_msg = await interaction.followup.send(
            chat_ack(prompt=agent_prompt.display, repo=repo)
        )
        job = FollowUpJob(
            agent_id=agent_id,
            agent_prompt=agent_prompt,
            repo=repo,
            project_alias=project_alias,
            status_message=status_msg,
            user_message=channel if isinstance(channel, discord.Thread) else None,
        )
        await self.bot.turns.submit_follow_up(
            job,
            start=self._start_queued_follow_up,
            ack=self._ack_queued_follow_up,
        )

    async def _follow_up_message(
        self,
        *,
        message: discord.Message,
        agent_id: str,
        agent_prompt: AgentPrompt,
        repo: str,
        project_alias: str | None,
    ) -> None:
        job = FollowUpJob(
            agent_id=agent_id,
            agent_prompt=agent_prompt,
            repo=repo,
            project_alias=project_alias,
            user_message=message,
        )
        await self.bot.turns.submit_follow_up(
            job,
            start=self._start_queued_follow_up,
            ack=self._ack_queued_follow_up,
        )

    async def _start_queued_follow_up(self, job: FollowUpJob) -> None:
        status_msg = job.status_message
        if status_msg is None:
            message = job.user_message
            if not isinstance(message, discord.Message):
                raise TypeError("queued follow-up needs user_message or status_message")
            status_msg = await message.reply(
                chat_ack(prompt=job.agent_prompt.display, repo=job.repo),
                mention_author=False,
            )
            job.status_message = status_msg
        if not isinstance(status_msg, discord.Message):
            raise TypeError("status_message must be a discord.Message")
        anchor = job.user_message
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=job.agent_id,
            agent_prompt=job.agent_prompt,
            repo=job.repo,
            project_alias=job.project_alias,
            anchor_message=anchor if isinstance(anchor, discord.Message) else None,
            coalesce_count=job.coalesce_count,
            coalesce_anchor=job.coalesce_anchor,
        )
        await self.bot.turns.flush_pending_turn(job.agent_id)

    async def _steer_follow_up(self, job: FollowUpJob) -> None:
        assert self.bot.user is not None
        anchor = job.user_message
        if not isinstance(anchor, discord.Message):
            status = job.status_message
            anchor = status if isinstance(status, discord.Message) else None
        channel: discord.abc.Messageable | None = None
        if isinstance(anchor, discord.Message):
            channel = anchor.channel
        elif isinstance(job.status_message, discord.Message):
            channel = job.status_message.channel
        before = _history_before_for_follow_up(
            coalesce_count=job.coalesce_count,
            coalesce_anchor=job.coalesce_anchor,
            anchor=anchor if isinstance(anchor, discord.Message) else None,
        )
        if channel is not None:
            augmented = await augment_prompt_with_thread(
                job.agent_prompt,
                channel=channel,
                before_message=before,
                bot_user_id=self.bot.user.id,
            )
        else:
            augmented = job.agent_prompt
        outcome = await self.bot.cursor.steer(
            agent_id=job.agent_id,
            prompt=augmented.sdk_message(),
        )
        if outcome.delivery in {"unavailable", "error"}:
            self.bot.turns.enqueue_pending(job)
            if anchor is not None and self.bot.turns.should_ack_queue(
                job.agent_id, anchor.channel.id
            ):
                await anchor.reply(
                    coalesce_ack(merged_count=job.coalesce_count),
                    mention_author=False,
                )
            return
        if anchor is not None and self.bot.turns.should_ack_queue(
            job.agent_id, anchor.channel.id
        ):
            await anchor.reply(steer_ack(outcome), mention_author=False)

    async def _ack_queued_follow_up(self, job: FollowUpJob, depth: int) -> None:
        anchor = job.user_message
        if not isinstance(anchor, discord.Message):
            status = job.status_message
            if isinstance(status, discord.Message):
                anchor = status
            else:
                return
        if not self.bot.turns.should_ack_queue(job.agent_id, anchor.channel.id):
            return
        await anchor.reply(
            coalesce_ack(merged_count=depth), mention_author=False
        )

    async def _drive_follow_up(
        self,
        *,
        status_message: discord.Message,
        agent_id: str,
        agent_prompt: AgentPrompt,
        repo: str,
        project_alias: str | None,
        anchor_message: discord.Message | None = None,
        coalesce_count: int = 1,
        coalesce_anchor: object | None = None,
    ) -> None:
        editor = ChatStatusEditor(status_message)
        assert self.bot.user is not None
        thread = status_message.channel
        if isinstance(thread, discord.Thread):
            self._bind_thread_agent(
                thread,
                agent_id=agent_id,
                repo=repo if repo != "(existing agent)" else None,
                project=project_alias,
            )
        history_before = _history_before_for_follow_up(
            coalesce_count=coalesce_count,
            coalesce_anchor=coalesce_anchor,
            anchor=anchor_message,
        )
        augmented = await augment_prompt_with_thread(
            agent_prompt,
            channel=status_message.channel,
            before_message=history_before or status_message,
            bot_user_id=self.bot.user.id,
        )

        async def on_status(state: str, data: dict) -> None:
            self.bot.sessions.set_status(agent_id, state)
            summary = data.get("snippet") or data.get("result") or data.get("error")
            err = data.get("error")
            if err:
                state = "error"
            if isinstance(thread, discord.Thread):
                self._bind_thread_agent(
                    thread,
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    repo=repo if repo != "(existing agent)" else None,
                    project=project_alias,
                )
            body = chat_status_body(
                agent_id=agent_id,
                status=state,
                repo=repo,
                snippet=data.get("snippet"),
                summary=summary,
                run_id=data.get("run_id"),
                pr_url=data.get("pr_url"),
                error=str(err) if err else None,
            )
            await editor.set_content(
                body, force=state not in {"running", "streaming"}
            )

        try:
            outcome = await self.bot.cursor.follow_up(
                agent_id=agent_id,
                prompt=augmented.sdk_message(),
                on_status=on_status,
            )
        except Exception as exc:  # noqa: BLE001
            if is_agent_busy_error(exc):
                raise AgentBusyError(str(exc)) from exc
            logger.exception("follow_up failed")
            await status_message.reply(
                f"Follow-up failed: `{exc}`", mention_author=False
            )
            return

        final = chat_status_body(
            agent_id=outcome.agent_id,
            status=outcome.status,
            repo=repo,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            error=outcome.error_message,
        )
        done_embed = completion_embed(
            agent_id=outcome.agent_id,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            pr_url=outcome.pr_url,
        )
        view = AgentHandoffView(
            agent_id=outcome.agent_id,
            pr_url=outcome.pr_url,
        )
        await editor.set_content(
            final, embed=done_embed, view=view, force=True
        )
        await _set_status_reaction(status_message, outcome.status)
        if isinstance(thread, discord.Thread):
            self._bind_thread_agent(
                thread,
                agent_id=outcome.agent_id,
                run_id=outcome.run_id,
                repo=repo if repo != "(existing agent)" else None,
                project=project_alias,
            )
            await self._deliver_artifacts(thread, outcome.agent_id)


# discord.Message uses __slots__, so the last reaction is tracked here by message id.
_status_emoji_by_message: dict[int, str] = {}


async def _set_status_reaction(message: discord.Message, status: str) -> None:
    key = (status or "").lower()
    if key in {"running", "streaming", "starting", "created"}:
        emoji = "⏳"
    elif key in {"error"}:
        emoji = "❌"
    else:
        emoji = "✅"
    if _status_emoji_by_message.get(message.id) == emoji:
        return
    try:
        await message.clear_reactions()
    except discord.HTTPException:
        pass
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        return
    if emoji == "⏳":
        _status_emoji_by_message[message.id] = emoji
    else:
        _status_emoji_by_message.pop(message.id, None)


def _history_before_for_follow_up(
    *,
    coalesce_count: int,
    coalesce_anchor: object | None,
    anchor: discord.Message | None,
) -> discord.Message | None:
    """When bursts are merged, omit those messages from thread context."""
    if coalesce_count > 1 and isinstance(coalesce_anchor, discord.Message):
        return coalesce_anchor
    return anchor


def _alias_role_mentions_to_bot(
    message: discord.Message, bot_user: discord.ClientUser, role_name: str
) -> None:
    """Treat @-mentions of the access role or the bot's own role as @-mentions of the bot.

    Discord autocompletes `@Cursor` to the access role when it shares the bot's name.
    """
    guild = message.guild
    if guild is None or not message.role_mentions:
        return
    self_role = guild.self_role
    hit = False
    content = message.content
    for role in message.role_mentions:
        if role.name == role_name or (self_role is not None and role.id == self_role.id):
            content = content.replace(f"<@&{role.id}>", f"<@{bot_user.id}>")
            hit = True
    if not hit:
        return
    message.content = content
    if bot_user not in message.mentions:
        message.mentions.append(bot_user)


def _strip_mention(content: str, bot_user: discord.ClientUser) -> str:
    text = content
    for form in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
        text = text.replace(form, "")
    return text.strip()


def _is_bot_mention_only(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    """True when the message is only an @Cursor ping with no prompt or files."""
    if message.attachments:
        return False
    return not _strip_mention(message.content, bot_user)


def _thread_name(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if len(cleaned) > 80:
        cleaned = cleaned[:77] + "…"
    return cleaned or "cursor-agent"


def _truncate_prompt(prompt: str) -> str:
    cleaned = " ".join((prompt or "").split())
    if len(cleaned) > 120:
        return cleaned[:117] + "…"
    return cleaned


async def _clear_deferred_thinking(
    interaction: discord.Interaction, placeholder: discord.Message
) -> None:
    """Finish the deferred interaction without relying on webhook message edits."""
    try:
        await interaction.delete_original_response()
        return
    except (discord.HTTPException, discord.NotFound):
        pass
    try:
        await interaction.followup.send(
            f"Started → {placeholder.jump_url}",
            ephemeral=True,
        )
    except discord.HTTPException:
        logger.debug("could not ack deferred interaction", exc_info=True)


async def setup(bot: CursorDiscordBot) -> None:
    await bot.add_cog(AgentCog(bot))
