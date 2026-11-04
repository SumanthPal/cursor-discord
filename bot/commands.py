from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.agent_ref import (
    build_starter_embed,
    find_agent_ref_in_channel,
    is_agent_id,
)
from bot.cloud_artifacts import deliver_new_cloud_artifacts
from bot.multimodal import AgentPrompt, agent_prompt_from_message
from bot.channel_bind import (
    pin_channel_repo,
    resolve_topic_channel,
    topic_from_channel,
)
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
from bot.ui import SetupView, controls_for, short_repo_label

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

# Discord message content hard limit (embeds use a higher cap).
DISCORD_CONTENT_MAX = 2000

HELP_TEXT = """**Cursor**
• `@Cursor …` or `/ask` to start (PNG/JPEG/GIF/WebP on the same message)
• Name the repo in chat: `@Cursor owner/repo do the thing` (or a GitHub URL)
• If several repos are allowed here, I'll ask — reply with `owner/repo` or a URL
• Works in channels an admin set up with `/setup`, on that channel's repos only
• Reply in the thread to steer · Cancel / Open PR on the card
• Demo screenshots/videos from the agent land in the thread when a run finishes (`/artifacts` to retry)

**Admins** (`SUPERUSER_IDS`)
• `/setup repos:owner/a,owner/b` binds this channel (`cursor-repo:…` in topic; needs Manage Channels)
• `/grant @member` / `/revoke @member` toggles the **{role}** role (needs Manage Roles)
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


def clip_discord(text: str, limit: int = DISCORD_CONTENT_MAX) -> str:
    """Keep a single Discord content body under the API limit."""
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(0, limit - len(suffix))] + suffix


def chunk_discord_lines(
    header: str,
    lines: list[str],
    *,
    footer: str = "",
    limit: int = DISCORD_CONTENT_MAX,
) -> list[str]:
    """Pack lines into Discord messages under `limit` chars each.

    When `footer` is set, every page reserves space for it so the last page
    can append the footer without dropping lines or spinning on rsplit.
    """
    # Reserve "\n" + footer on every page so the last page can append safely.
    footer_room = (1 + len(footer)) if footer else 0
    body_limit = max(1, limit - footer_room)
    chunks: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= body_limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = clip_discord(line, body_limit) if len(line) > body_limit else line
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [header or "_(empty)_"]
    if footer:
        last = chunks[-1]
        joined = f"{last}\n{footer}" if last else footer
        chunks[-1] = clip_discord(joined, limit)
    return [clip_discord(c, limit) for c in chunks]


def display_project_alias(alias: str | None) -> str | None:
    """Hide internal resolve aliases from Discord chrome."""
    if not alias or alias.startswith("_"):
        return None
    return alias


class AgentCog(commands.Cog):
    def __init__(self, bot: CursorDiscordBot) -> None:
        self.bot = bot

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
            HELP_TEXT.format(role=self.bot.settings.cursor_role_name), ephemeral=True
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
        role = discord.utils.get(guild.roles, name=name)
        try:
            if grant:
                if role is None:
                    role = await guild.create_role(
                        name=name, reason="Cursor bot access role"
                    )
                await member.add_roles(role, reason=f"/grant by {interaction.user}")
            elif role is not None:
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
        if self.bot.policy.is_admin(member) and not grant:
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
            ref = await find_agent_ref_in_channel(
                channel, bot_user_id=self.bot.user.id
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.bot.user is None:
            return

        # Plain reply naming a repo after we asked "which repo?"
        if await self._maybe_consume_repo_reply(message):
            return

        # Thread follow-ups to an existing agent
        if isinstance(message.channel, discord.Thread):
            ref = await find_agent_ref_in_channel(
                message.channel, bot_user_id=self.bot.user.id
            )
            if ref and not _is_bot_mention_only(message, self.bot.user):
                prompt_text = message.content.strip()
                if self.bot.user in message.mentions:
                    prompt_text = _strip_mention(message.content, self.bot.user)
                if (
                    (prompt_text or message.attachments)
                    and not message.content.startswith("/")
                ):
                    denied = self.bot.policy.authorize(
                        user=message.author, guild=message.guild
                    )
                    if denied:
                        await message.reply(denied, mention_author=False)
                        return
                    agent_prompt = await agent_prompt_from_message(
                        message, text=prompt_text
                    )
                    if not agent_prompt.has_content:
                        if agent_prompt.feedback:
                            await message.reply(
                                clip_discord(agent_prompt.feedback),
                                mention_author=False,
                            )
                        return
                    await self._follow_up_message(
                        message=message,
                        agent_id=ref.agent_id,
                        agent_prompt=agent_prompt,
                        repo=ref.repo or "(existing agent)",
                        project_alias=ref.project,
                    )
                    return

        if self.bot.user not in message.mentions:
            return
        prompt_text = _strip_mention(message.content, self.bot.user)
        # `@Cursor owner/repo do the thing` — peel repo off the front.
        inline_repo, prompt_text = split_repo_prefix(prompt_text)
        agent_prompt = await agent_prompt_from_message(message, text=prompt_text)
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
            user=message.author, channel=message.channel, repo=inline_repo
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
            )
            return

        # Admin named a repo in an unbound channel — pin it like the old first-pick.
        if (
            inline_repo
            and self.bot.policy.is_admin(message.author)
            and not await self._channel_projects(message.channel)
        ):
            pin_note = await pin_channel_repo(
                self.bot, message.channel, [plan.repo]
            )
            await message.reply(pin_note, mention_author=False)

        await self._launch_new_agent_from_message(
            message=message, agent_prompt=agent_prompt, project=plan
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

    async def _plan_launch(
        self,
        *,
        user: discord.abc.User,
        channel: discord.abc.Messageable | None,
        repo: str | None,
    ) -> Project | RepoChoice | str:
        """Project to launch, a picker to show, or a user-facing refusal."""
        bound = await self._channel_projects(channel)
        admin = self.bot.policy.is_admin(user)
        model = self.bot.settings.cursor_model

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
    ) -> None:
        if channel is None or not hasattr(channel, "id"):
            return
        channel_id = int(channel.id)
        self.bot.pending_repo_asks[self._pending_key(channel_id, user.id)] = (
            PendingRepoAsk(
                agent_prompt=agent_prompt,
                choice=choice,
                owner_id=user.id,
                channel_id=channel_id,
                expires_at=time.monotonic() + PENDING_REPO_TTL_SEC,
            )
        )
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
        await channel.send("\n".join(lines))

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
        await self._launch_new_agent_from_message(
            message=message,
            agent_prompt=pending.agent_prompt,
            project=project,
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

        embed = build_starter_embed(
            agent_id="pending",
            prompt=agent_prompt.display,
            project=display_project_alias(project.alias),
            repo=project.repo,
            status="starting",
        )
        # Acknowledge the interaction (clears Discord's 3s / thinking UI), but
        # post the status card via channel.send so later edits use the bot
        # token — interaction webhook tokens expire after ~15 minutes.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        placeholder = await channel.send(embed=embed)
        await _clear_deferred_thinking(interaction, placeholder)
        thread = await self._ensure_thread(channel, placeholder, agent_prompt.display)
        await self._run_create(
            status_message=placeholder,
            thread=thread,
            agent_prompt=agent_prompt,
            project=project,
        )

    async def _launch_new_agent_from_message(
        self,
        *,
        message: discord.Message,
        agent_prompt: AgentPrompt,
        project: Project,
    ) -> None:
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await message.reply("Need a text channel.", mention_author=False)
            return
        placeholder = await message.reply(
            embed=build_starter_embed(
                agent_id="pending",
                prompt=agent_prompt.display,
                project=display_project_alias(project.alias),
                repo=project.repo,
                status="starting",
            ),
            mention_author=False,
        )
        thread = await self._ensure_thread(channel, placeholder, agent_prompt.display)
        await self._run_create(
            status_message=placeholder,
            thread=thread,
            agent_prompt=agent_prompt,
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
        agent_prompt: AgentPrompt,
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
                prompt=agent_prompt.display,
                project=display_project_alias(project.alias),
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
                prompt=agent_prompt.sdk_message(),
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
            prompt=agent_prompt.display,
            project=display_project_alias(project.alias),
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

        bits = [f"**{outcome.status.title()}.** `{outcome.agent_id}` — reply to continue."]
        if outcome.pr_url:
            bits.append(outcome.pr_url)
        await thread.send(" ".join(bits))
        await self._deliver_artifacts(thread, outcome.agent_id)

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
        status_msg = await interaction.followup.send(
            embed=build_starter_embed(
                agent_id=agent_id,
                prompt=agent_prompt.display,
                project=project_alias,
                repo=repo,
                status="starting",
            )
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            agent_prompt=agent_prompt,
            repo=repo,
            project_alias=project_alias,
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
        status_msg = await message.reply(
            embed=build_starter_embed(
                agent_id=agent_id,
                prompt=agent_prompt.display,
                project=project_alias,
                repo=repo,
                status="starting",
            ),
            mention_author=False,
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            agent_prompt=agent_prompt,
            repo=repo,
            project_alias=project_alias,
        )

    async def _drive_follow_up(
        self,
        *,
        status_message: discord.Message,
        agent_id: str,
        agent_prompt: AgentPrompt,
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
                prompt=agent_prompt.display,
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
                agent_id=agent_id,
                prompt=agent_prompt.sdk_message(),
                on_status=on_status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("follow_up failed")
            await status_message.reply(
                f"Follow-up failed: `{exc}`", mention_author=False
            )
            return

        embed = build_starter_embed(
            agent_id=outcome.agent_id,
            prompt=agent_prompt.display,
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

        channel = status_message.channel
        if isinstance(channel, discord.Thread):
            await self._deliver_artifacts(channel, outcome.agent_id)


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
    cleaned = " ".join(prompt.split())
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
