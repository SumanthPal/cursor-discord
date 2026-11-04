from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.agent_ref import (
    find_agent_ref_in_channel,
    is_agent_id,
    ref_spoiler,
)
from bot.chat_ui import (
    ChatStatusEditor,
    PendingRepoLaunch,
    chat_ack,
    chat_failed,
    chat_status_body,
    format_repo_choice_prompt,
    resolve_repo_from_reply,
)
from bot.cloud_artifacts import deliver_new_cloud_artifacts
from bot.multimodal import AgentPrompt, agent_prompt_from_message
from bot.projects import Project, parse_channel_repo, set_channel_project_topic
from bot.projects import set_channel_repo_topic as pin_channel_repo
from bot.repo_refs import decode_repo_ref, encode_repo_ref
from bot.turn_coordinator import (
    AgentBusyError,
    FollowUpJob,
    chat_queue_ack,
    is_agent_busy_error,
)
from bot.ui import short_repo_label

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

# Discord message content hard limit (embeds use a higher cap).
DISCORD_CONTENT_MAX = 2000

HELP_TEXT = """**Cursor** — chat with a cloud coding agent
• `@Cursor …` or `/ask` — say what you want (images on the same message work too)
• I branch a thread from your `@Cursor` message; keep talking there — I queue if I'm still busy
• No repo set on the channel yet? I'll ask which one — reply with the number or name
• `/cancel` stops a run · PR links show up in my reply when one is opened
• Demo screenshots/videos land in the thread when a run finishes (`/artifacts` to retry)
"""


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
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)

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
        resolved = self._resolve_project(
            project=None,
            repo=resolved_repo,
            branch=None,
            channel=interaction.channel,
        )
        if resolved is None:
            await self._offer_repo_picker(
                interaction=interaction,
                agent_prompt=AgentPrompt.from_text(prompt),
                branch=None,
            )
            return

        await self._launch_new_agent(
            interaction=interaction,
            agent_prompt=AgentPrompt.from_text(prompt),
            project=resolved,
            announce_channel=interaction.channel,
        )

    @ask_cmd.autocomplete("repo")
    async def ask_repo_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            urls = await self.bot.cursor.list_repos()
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
        description="List GitHub repos connected to your Cursor API key",
    )
    async def repos_cmd(self, interaction: discord.Interaction) -> None:
        denied = self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild, consume_rate=False
        )
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            urls = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Could not list repos (is CURSOR_API_KEY valid / GitHub linked?): {exc}",
                ephemeral=True,
            )
            return
        if not urls:
            await interaction.followup.send(
                "No repos returned. Connect GitHub in the Cursor dashboard "
                "for the account that owns this API key.",
                ephemeral=True,
            )
            return
        # Compact labels only — full URLs blow past Discord's 2000-char content cap.
        lines = [f"• `{short_repo_label(u)}`" for u in urls]
        pages = chunk_discord_lines(
            f"**{len(urls)} Cursor-connected repo(s)**",
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

        pending_key = (message.channel.id, message.author.id)
        pending = self.bot.pending_repo_launches.get(pending_key)
        if pending is not None and isinstance(pending, PendingRepoLaunch):
            if pending.owner_id != message.author.id:
                return
            denied = self.bot.policy.authorize(
                user=message.author, guild=message.guild
            )
            if denied:
                await message.reply(denied, mention_author=False)
                return
            picked = resolve_repo_from_reply(message.content, pending.urls)
            if not picked:
                await message.reply(
                    "I didn't catch that — reply with the **number** from the list "
                    "or a repo name like `org/repo`.",
                    mention_author=False,
                )
                return
            del self.bot.pending_repo_launches[pending_key]
            await self._maybe_pin_channel_repo(message.channel, picked)
            project = Project(
                alias="_explicit",
                repo=picked,
                branch=(pending.branch or "").strip() or None,
                model=self.bot.settings.cursor_model,
            )
            try:
                anchor = await message.channel.fetch_message(
                    pending.thread_anchor_message_id
                )
            except discord.NotFound:
                await message.reply(
                    "Couldn't find the original ask — `@Cursor` again to restart.",
                    mention_author=False,
                )
                return
            await self._launch_new_agent_from_message(
                message=message,
                agent_prompt=pending.agent_prompt,  # type: ignore[arg-type]
                project=project,
                thread_anchor=anchor,
            )
            return

        if self.bot.user not in message.mentions:
            return
        prompt_text = _strip_mention(message.content, self.bot.user)
        agent_prompt = await agent_prompt_from_message(message, text=prompt_text)
        if not agent_prompt.has_content:
            if agent_prompt.feedback:
                await message.reply(
                    clip_discord(agent_prompt.feedback),
                    mention_author=False,
                )
            else:
                await message.reply(
                    "`@Cursor <what to do>` (images can be attached) or `/ask`",
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
            await self._offer_repo_picker_message(
                message=message, agent_prompt=agent_prompt
            )
            return

        await self._launch_new_agent_from_message(
            message=message, agent_prompt=agent_prompt, project=resolved
        )

    async def _offer_repo_picker(
        self,
        *,
        interaction: discord.Interaction,
        agent_prompt: AgentPrompt,
        branch: str | None,
    ) -> None:
        # Defer before list_repos — Cursor API can exceed Discord's 3s window.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            urls = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                f"Could not load Cursor repos: {exc}",
                ephemeral=True,
            )
            return
        if not urls:
            await interaction.followup.send(
                "No Cursor-connected repos found. Link GitHub in the Cursor "
                "dashboard for this API key, or pass `repo:` on `/ask`.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "Need a text channel to chat.", ephemeral=True
            )
            return

        prompt_text = format_repo_choice_prompt(
            urls, task_preview=agent_prompt.display
        )
        posted = await channel.send(prompt_text)
        self.bot.pending_repo_launches[(channel.id, interaction.user.id)] = (
            PendingRepoLaunch(
                agent_prompt=agent_prompt,
                urls=urls,
                branch=branch,
                owner_id=interaction.user.id,
                channel_id=channel.id,
                thread_anchor_message_id=posted.id,
            )
        )
        await _clear_deferred_thinking(interaction, posted)

    async def _offer_repo_picker_message(
        self, *, message: discord.Message, agent_prompt: AgentPrompt
    ) -> None:
        try:
            urls = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            await message.reply(
                f"Could not load Cursor repos: `{exc}`", mention_author=False
            )
            return
        if not urls:
            await message.reply(
                "No Cursor-connected repos. Link GitHub in Cursor, or `/ask` + `repo:`.",
                mention_author=False,
            )
            return

        self.bot.pending_repo_launches[(message.channel.id, message.author.id)] = (
            PendingRepoLaunch(
                agent_prompt=agent_prompt,
                urls=urls,
                branch=None,
                owner_id=message.author.id,
                channel_id=message.channel.id,
                thread_anchor_message_id=message.id,
            )
        )
        await message.reply(
            format_repo_choice_prompt(urls, task_preview=agent_prompt.display),
            mention_author=False,
        )

    async def _maybe_pin_channel_repo(
        self, channel: discord.abc.Messageable, repo_url: str
    ) -> None:
        topic_channel = channel
        if isinstance(channel, discord.Thread) and isinstance(
            channel.parent, discord.TextChannel
        ):
            topic_channel = channel.parent
        if not isinstance(topic_channel, discord.TextChannel):
            return
        if parse_channel_repo(topic_channel.topic):
            return
        if not topic_channel.permissions_for(topic_channel.guild.me).manage_channels:
            return
        try:
            await topic_channel.edit(
                topic=pin_channel_repo(topic_channel.topic, repo_url)
            )
        except discord.HTTPException:
            logger.warning("channel repo pin failed", exc_info=True)

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
        )

    async def _launch_new_agent_from_message(
        self,
        *,
        message: discord.Message,
        agent_prompt: AgentPrompt,
        project: Project,
        thread_anchor: discord.Message | None = None,
    ) -> None:
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
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
        )

    async def _open_agent_thread(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        prompt: str,
        repo: str | None,
        thread_anchor: discord.Message | None = None,
        interaction_user: discord.User | discord.Member | None = None,
    ) -> tuple[discord.Thread, discord.Message]:
        """Branch a public thread from the anchor message; first bot reply lives inside the thread."""
        if isinstance(channel, discord.Thread):
            status_message = await channel.send(
                chat_ack(prompt=prompt, repo=repo, echo_prompt=False)
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
        thread: discord.Thread,
        agent_prompt: AgentPrompt,
        project: Project,
    ) -> None:
        editor = ChatStatusEditor(status_message)

        async def on_status(state: str, data: dict) -> None:
            agent_id = data.get("agent_id") or "pending"
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
            await editor.set_content(
                body, force=state not in {"running", "streaming"}
            )

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
            await thread.send(chat_failed(exc))
            return

        final = chat_status_body(
            agent_id=outcome.agent_id,
            status=outcome.status,
            repo=project.repo,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            error=outcome.error_message,
        )
        await editor.set_content(final, force=True)
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
        channel = interaction.channel
        status_msg = await interaction.followup.send(
            chat_ack(prompt=agent_prompt.display, repo=repo)
            + "\n"
            + ref_spoiler(agent_id)
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
                chat_ack(prompt=job.agent_prompt.display, repo=job.repo)
                + "\n"
                + ref_spoiler(job.agent_id),
                mention_author=False,
            )
        if not isinstance(status_msg, discord.Message):
            raise TypeError("status_message must be a discord.Message")
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=job.agent_id,
            agent_prompt=job.agent_prompt,
            repo=job.repo,
            project_alias=job.project_alias,
        )

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
        await anchor.reply(chat_queue_ack(depth=depth), mention_author=False)

    async def _drive_follow_up(
        self,
        *,
        status_message: discord.Message,
        agent_id: str,
        agent_prompt: AgentPrompt,
        repo: str,
        project_alias: str | None,
    ) -> None:
        editor = ChatStatusEditor(status_message)

        async def on_status(state: str, data: dict) -> None:
            summary = data.get("snippet") or data.get("result") or data.get("error")
            err = data.get("error")
            if err:
                state = "error"
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
                prompt=agent_prompt.sdk_message(),
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
        await editor.set_content(final, force=True)

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
