from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot.agent_ref import (
    chat_done,
    chat_failed,
    chat_progress,
    chat_working,
    find_agent_ref_in_channel,
    is_agent_id,
)
from bot.projects import Project, set_channel_project_topic
from bot.repo_refs import decode_repo_ref, encode_repo_ref
from bot.ui import RepoPickerView, controls_for, short_repo_label

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

# Discord message content hard limit (embeds use a higher cap).
DISCORD_CONTENT_MAX = 2000
# Don't spam Discord while the cloud agent is cloning / thinking.
_PROGRESS_MIN_INTERVAL_SEC = 20.0

HELP_TEXT = """**Clippy**
• `@Clippy …` or `/ask` to start → opens a thread
• Wait for **your turn** in that thread, then reply (no @) to steer
• First repo pick pins the channel (`cursor-repo:…`)
"""


class _RunBoard:
    """Edits one Discord message through on-it → still-working → your-turn."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        message: discord.Message,
        *,
        repo: str | None,
    ) -> None:
        self.bot = bot
        self.message = message
        self.repo = repo
        self.agent_id = "pending"
        self._last_progress_at = 0.0
        self._last_text = message.content or ""

    async def set_working(self, *, agent_id: str | None = None) -> None:
        if agent_id and is_agent_id(agent_id):
            self.agent_id = agent_id
        text = chat_working(repo=self.repo, agent_id=self.agent_id if is_agent_id(self.agent_id) else None)
        view = controls_for(self.bot, agent_id=self.agent_id, status="running")
        await self._edit(text, view=view)

    async def maybe_progress(
        self,
        *,
        agent_id: str | None,
        run_id: str | None,
        snippet: str | None,
        force: bool = False,
    ) -> None:
        if agent_id and is_agent_id(agent_id):
            self.agent_id = agent_id
        now = time.monotonic()
        if not force and (now - self._last_progress_at) < _PROGRESS_MIN_INTERVAL_SEC:
            return
        self._last_progress_at = now
        text = chat_progress(
            snippet=snippet,
            agent_id=self.agent_id if is_agent_id(self.agent_id) else None,
            run_id=run_id,
        )
        view = controls_for(self.bot, agent_id=self.agent_id, status="running")
        await self._edit(text, view=view)

    async def finish(
        self,
        *,
        agent_id: str,
        status: str,
        summary: str | None,
        run_id: str | None,
        pr_url: str | None,
    ) -> None:
        self.agent_id = agent_id
        text = chat_done(
            agent_id=agent_id,
            status=status,
            summary=summary,
            run_id=run_id,
            pr_url=pr_url,
            repo=self.repo,
        )
        view = controls_for(
            self.bot, agent_id=agent_id, status=status, pr_url=pr_url
        )
        await self._edit(text, view=view)

    async def fail(self, exc: object) -> None:
        await self._edit(chat_failed(exc), view=None)

    async def _edit(
        self, content: str, *, view: discord.ui.View | None
    ) -> None:
        if content == self._last_text and view is None:
            return
        self._last_text = content
        try:
            await self.message.edit(content=content, embed=None, view=view)
        except discord.HTTPException:
            logger.warning("failed to edit run board", exc_info=True)


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

    @app_commands.command(name="help", description="How to use Clippy")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)

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
                prompt=prompt,
                branch=None,
            )
            return

        await self._launch_new_agent(
            interaction=interaction,
            prompt=prompt,
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
            footer="`@Clippy …` or `/ask` to start.",
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
                "No nicknames. Use `@Clippy` / `/ask` — first pick pins the channel.",
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
        try:
            await self._on_message_inner(message)
        except Exception:
            logger.exception(
                "on_message failed (author=%s channel=%s)",
                getattr(message.author, "id", None),
                getattr(message.channel, "id", None),
            )

    async def _on_message_inner(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.bot.user is None:
            return

        # Thread follow-ups to an existing agent (any non-empty reply)
        if isinstance(message.channel, discord.Thread):
            ref = await find_agent_ref_in_channel(
                message.channel, bot_user_id=self.bot.user.id
            )
            if ref and not _is_bot_mention_only(message, self.bot.user):
                prompt = message.content.strip()
                if prompt and not message.content.startswith("/"):
                    # Prefer starting a new agent when they clearly @Clippy.
                    if not _message_addresses_bot(message, self.bot.user):
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

        if not _message_addresses_bot(message, self.bot.user):
            return

        # Ping with empty body ⇒ Message Content Intent usually off in the portal
        # (slash commands still work — they don't need that intent).
        if not (message.content or "").strip() and self.bot.user in message.mentions:
            try:
                await message.reply(
                    "I saw the ping but no message text. Enable **Message Content "
                    "Intent** (Developer Portal → Bot → Privileged Gateway Intents), "
                    "save, then `@Clippy …` again. `/ask` keeps working either way.",
                    mention_author=False,
                )
            except discord.HTTPException:
                logger.warning("could not warn about message content intent", exc_info=True)
            return

        prompt = _strip_bot_address(message.content, self.bot.user)
        if not prompt:
            try:
                await message.reply(
                    "`@Clippy <what to do>` or `/ask`",
                    mention_author=False,
                )
            except discord.HTTPException:
                logger.warning("could not reply to empty @Clippy", exc_info=True)
            return

        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            await message.reply(denied, mention_author=False)
            return

        logger.info(
            "@Clippy from %s in #%s: %s",
            message.author.id,
            getattr(message.channel, "name", message.channel.id),
            _truncate_prompt(prompt),
        )

        resolved = self._resolve_project(
            project=None,
            repo=None,
            branch=None,
            channel=message.channel,
        )
        if resolved is None:
            await self._offer_repo_picker_message(message=message, prompt=prompt)
            return

        await self._launch_new_agent_from_message(
            message=message, prompt=prompt, project=resolved
        )

    async def _offer_repo_picker(
        self,
        *,
        interaction: discord.Interaction,
        prompt: str,
        branch: str | None,
    ) -> None:
        # Defer before list_repos — Cursor API can exceed Discord's 3s window.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
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
                "dashboard for this API key, or pass `repo:` explicitly.",
                ephemeral=True,
            )
            return

        async def on_picked(
            pick_interaction: discord.Interaction, project: Project
        ) -> None:
            await self._launch_new_agent(
                interaction=pick_interaction,
                prompt=prompt,
                project=project,
                announce_channel=interaction.channel,
            )

        view = RepoPickerView(
            self.bot,
            prompt=prompt,
            repo_urls=urls,
            branch=branch,
            on_picked=on_picked,
            owner_id=interaction.user.id,
            channel=interaction.channel,
        )
        await interaction.followup.send(
            f"Pick a repo for: **{_truncate_prompt(prompt)}**",
            view=view,
            ephemeral=True,
        )

    async def _offer_repo_picker_message(
        self, *, message: discord.Message, prompt: str
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

        async def on_picked(
            pick_interaction: discord.Interaction, project: Project
        ) -> None:
            await self._launch_new_agent(
                interaction=pick_interaction,
                prompt=prompt,
                project=project,
                announce_channel=message.channel,
            )

        view = RepoPickerView(
            self.bot,
            prompt=prompt,
            repo_urls=urls,
            branch=None,
            on_picked=on_picked,
            owner_id=message.author.id,
            channel=message.channel,
        )
        await message.reply(
            f"Pick a repo for: **{_truncate_prompt(prompt)}**",
            view=view,
            mention_author=False,
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

        # Channel message (bot token) so edits outlive the ~15m interaction webhook.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        placeholder = await channel.send(chat_working(repo=project.repo))
        await _clear_deferred_thinking(interaction, placeholder)
        thread = await self._ensure_thread(channel, placeholder, prompt)
        await thread.send(
            "I'll post updates here. When you see **your turn**, reply to steer."
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
            chat_working(repo=project.repo),
            mention_author=False,
        )
        thread = await self._ensure_thread(channel, placeholder, prompt)
        if thread.id != getattr(channel, "id", None):
            await thread.send(
                "I'll post updates here. When you see **your turn**, reply to steer."
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
        board = _RunBoard(self.bot, status_message, repo=project.repo)

        async def on_status(state: str, data: dict) -> None:
            agent_id = data.get("agent_id")
            if data.get("error"):
                await board.finish(
                    agent_id=agent_id or board.agent_id,
                    status="error",
                    summary=str(data["error"]),
                    run_id=data.get("run_id"),
                    pr_url=data.get("pr_url"),
                )
                return
            key = (state or "").lower()
            if key in {"created", "starting"}:
                await board.set_working(agent_id=agent_id)
            elif key == "running":
                await board.maybe_progress(
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    snippet=None,
                )
            elif key == "streaming":
                await board.maybe_progress(
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    snippet=data.get("snippet") or data.get("result"),
                )

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
            await board.fail(exc)
            await thread.send(chat_failed(exc))
            return

        await board.finish(
            agent_id=outcome.agent_id,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
        )
        # Thread bump so it's obvious even if the starter message scrolled away.
        cue = chat_done(
            agent_id=outcome.agent_id,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
            repo=project.repo,
        )
        view = controls_for(
            self.bot,
            agent_id=outcome.agent_id,
            status=outcome.status,
            pr_url=outcome.pr_url,
        )
        await thread.send(cue, view=view)

    async def _follow_up(
        self,
        *,
        interaction: discord.Interaction,
        agent_id: str,
        prompt: str,
        repo: str,
        project_alias: str | None,
    ) -> None:
        _ = project_alias
        status_msg = await interaction.followup.send(
            chat_working(repo=repo, agent_id=agent_id)
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            prompt=prompt,
            repo=repo,
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
        _ = project_alias
        status_msg = await message.reply(
            chat_working(repo=repo, agent_id=agent_id),
            mention_author=False,
        )
        await self._drive_follow_up(
            status_message=status_msg,
            agent_id=agent_id,
            prompt=prompt,
            repo=repo,
        )

    async def _drive_follow_up(
        self,
        *,
        status_message: discord.Message,
        agent_id: str,
        prompt: str,
        repo: str,
    ) -> None:
        board = _RunBoard(self.bot, status_message, repo=repo)
        board.agent_id = agent_id

        async def on_status(state: str, data: dict) -> None:
            if data.get("error"):
                await board.finish(
                    agent_id=agent_id,
                    status="error",
                    summary=str(data["error"]),
                    run_id=data.get("run_id"),
                    pr_url=data.get("pr_url"),
                )
                return
            key = (state or "").lower()
            if key in {"created", "starting"}:
                await board.set_working(agent_id=agent_id)
            elif key == "running":
                await board.maybe_progress(
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    snippet=None,
                )
            elif key == "streaming":
                await board.maybe_progress(
                    agent_id=agent_id,
                    run_id=data.get("run_id"),
                    snippet=data.get("snippet") or data.get("result"),
                )

        try:
            outcome = await self.bot.cursor.follow_up(
                agent_id=agent_id, prompt=prompt, on_status=on_status
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("follow_up failed")
            await board.fail(exc)
            return

        await board.finish(
            agent_id=outcome.agent_id,
            status=outcome.status,
            summary=outcome.result_text or outcome.error_message,
            run_id=outcome.run_id,
            pr_url=outcome.pr_url,
        )


def _message_addresses_bot(
    message: discord.Message, bot_user: discord.ClientUser
) -> bool:
    """True for a real ping or a soft `@Clippy …` / `Clippy, …` address."""
    if bot_user in message.mentions:
        return True
    content = message.content or ""
    if f"<@{bot_user.id}>" in content or f"<@!{bot_user.id}>" in content:
        return True
    # Soft address when someone types the name without picking the mention chip.
    name = (bot_user.display_name or bot_user.name or "clippy").lower()
    lowered = content.lstrip().lower()
    return lowered.startswith(f"@{name}") or lowered.startswith(f"{name} ")


def _strip_bot_address(content: str, bot_user: discord.ClientUser) -> str:
    text = content or ""
    for form in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
        text = text.replace(form, "")
    name = (bot_user.display_name or bot_user.name or "Clippy").strip()
    # Strip leading @Name / Name,
    lowered = text.lstrip()
    for prefix in (f"@{name}", name):
        if lowered.lower().startswith(prefix.lower()):
            lowered = lowered[len(prefix) :]
            break
    return lowered.lstrip(" ,:").strip()


def _strip_mention(content: str, bot_user: discord.ClientUser) -> str:
    # Back-compat alias
    return _strip_bot_address(content, bot_user)


def _is_bot_mention_only(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    return not _strip_bot_address(message.content, bot_user)


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
