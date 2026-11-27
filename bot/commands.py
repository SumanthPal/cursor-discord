"""The whole conversation: @Cursor in a channel starts an agent in a thread, replies continue it.

No slash commands. A few short phrasings are recognized (stop, "use owner/repo here",
"give @someone access"); everything else goes to the agent.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot import chat_ui
from bot.agent_ref import find_agent_in_thread
from bot.cloud_artifacts import deliver_new_cloud_artifacts
from bot.cursor_agent import TurnResult, is_busy_error, is_not_found_error
from bot.cursor_parse import Intent, Parsed, classify, is_stop, strip_bot_mention
from bot.multimodal import AgentPrompt, agent_prompt_from_message, merge_agent_prompts
from bot.repos import (
    channel_repos,
    compact_repo,
    find_repo_in_text,
    leading_repo_name,
    format_repos,
    match_allowed_repo,
    same_repo,
    save_channel_repos,
)

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

REPO_QUESTION_TTL_SEC = 300
SEEN = "👀"

HELLO = (
    "Hi! Tell me what you need, like `@Cursor fix the flaky login test`, and I'll work "
    "on it in a thread. Screenshots help. Reply in the thread to keep going, or say "
    "*stop*. If you want a different repo, just name it (`owner/repo`)."
)
HELLO_ADMIN = (
    "\n\nSince you're an admin: `@Cursor use owner/repo here` sets which repos this channel "
    "works on, and `@Cursor give @someone access` lets someone use me."
)
ONBOARDING = (
    "Thanks for adding me! Admins, go to a channel and say `@Cursor use owner/repo here`. "
    "Then `@Cursor give @someone access` for each person who should be able to use me. "
    "After that, anyone with access can ask me for things in that channel.\n\n"
    "I need **Manage Channels** (I keep repo settings in the channel topic), **Create "
    "Public Threads**, and **Manage Roles** with my role above **{role}**."
)
OPEN_MODE_WARNING = (
    "\n\n**Heads up:** `SUPERUSER_IDS` isn't set, so everyone here counts as an admin."
)
NOT_SET_UP = (
    "This channel doesn't have a repo yet. An admin can set one with "
    "`@Cursor use owner/repo here`."
)


@dataclass
class ThreadState:
    """An agent thread. ``agent_id`` is None until Cursor creates the agent."""

    agent_id: str | None = None
    busy: bool = False
    pending: list[AgentPrompt] = field(default_factory=list)
    # Repo the thread was launched in (unknown for threads recovered after a redeploy).
    # With no live agent, the next reply relaunches here.
    repo: str | None = None

    @property
    def is_agent(self) -> bool:
        return self.agent_id is not None or self.busy or self.repo is not None


@dataclass
class RepoQuestion:
    """We asked "which repo?" and are waiting for this user's answer."""

    message: discord.Message
    prompt: AgentPrompt
    choices: list[str]
    remember: bool  # admin in a channel with no repos yet → save the answer
    expires_at: float


class AgentCog(commands.Cog):
    def __init__(self, bot: CursorDiscordBot) -> None:
        self.bot = bot
        self.threads: dict[int, ThreadState] = {}
        self.repo_questions: dict[tuple[int, int], RepoQuestion] = {}
        self.posted_artifacts: dict[str, set[str]] = {}

    @property
    def me(self) -> discord.ClientUser:
        assert self.bot.user is not None
        return self.bot.user

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return
        text = ONBOARDING.format(role=self.bot.settings.cursor_role_name)
        if self.bot.policy.open_mode:
            text += OPEN_MODE_WARNING
        await channel.send(text)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None or message.guild is None:
            return
        _alias_role_mentions_to_bot(message, self.me, self.bot.settings.cursor_role_name)
        mentioned = self.me in message.mentions
        text = strip_bot_mention(message.content, self.me.id)

        if isinstance(message.channel, discord.Thread):
            state = await self._agent_thread(message.channel)
            if state is not None:
                await self._on_thread_message(message, state, text, mentioned)
                return
        if not mentioned:
            await self._maybe_answer_repo_question(message)
            return
        await self._on_mention(message, text)

    # --- talking in an agent thread -------------------------------------------------

    async def _on_thread_message(
        self, message: discord.Message, state: ThreadState, text: str, mentioned: bool
    ) -> None:
        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            # Threads are public; only answer people who were talking to us.
            if mentioned:
                await message.reply(denied, mention_author=False)
            return
        if not text and not message.attachments:
            return
        if is_stop(text):
            await self._stop(message, state)
            return
        if mentioned and await self._handle_admin_phrase(message, text):
            return

        prompt = await agent_prompt_from_message(message, text=text)
        if not prompt.has_content:
            if prompt.feedback:
                await message.reply(prompt.feedback, mention_author=False)
            return

        # Mid-run: steer it if we can, otherwise pick it up right after. Re-check
        # busy after the steer call, since the run may have finished meanwhile.
        if state.busy and state.agent_id and await self._try_steer(state.agent_id, prompt):
            await _react(message, SEEN)
            return
        if state.busy:
            state.pending.append(prompt)
            await _react(message, SEEN)
            return

        state.busy = True  # claim the thread before any await
        try:
            status = await message.reply(chat_ui.working(), mention_author=False)
        except discord.HTTPException:
            state.busy = False
            raise
        await self._converse(message.channel, state, prompt, status=status, author=message.author)

    async def _stop(self, message: discord.Message, state: ThreadState) -> None:
        state.pending.clear()
        if state.agent_id is None:
            await message.reply("Still starting up. Say stop again in a second.", mention_author=False)
            return
        try:
            stopped = await self.bot.cursor.cancel(state.agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cancel failed for %s", state.agent_id)
            await message.reply(f"I couldn't stop it: {exc}", mention_author=False)
            return
        if stopped:
            await _react(message, "👍")
        else:
            await message.reply("Nothing's running right now.", mention_author=False)

    async def _try_steer(self, agent_id: str, prompt: AgentPrompt) -> bool:
        try:
            return await self.bot.cursor.steer(agent_id, prompt.sdk_message())
        except Exception:  # noqa: BLE001
            logger.warning("steer failed for %s", agent_id, exc_info=True)
            return False

    async def _agent_thread(self, thread: discord.Thread) -> ThreadState | None:
        state = self.threads.get(thread.id)
        if state is None:
            # Not seen since startup: recover the agent id from the thread itself.
            agent_id = await find_agent_in_thread(thread, bot_user_id=self.me.id)
            state = self.threads[thread.id] = ThreadState(agent_id=agent_id)
        return state if state.is_agent else None

    # --- @Cursor in a channel -------------------------------------------------------

    async def _on_mention(self, message: discord.Message, text: str) -> None:
        self.repo_questions.pop((message.channel.id, message.author.id), None)
        parsed = classify(
            text,
            mentions_people=any(m.id != self.me.id for m in message.mentions),
            has_files=bool(message.attachments),
        )
        if parsed.intent == Intent.HELLO:
            hello = HELLO + (HELLO_ADMIN if self.bot.policy.is_admin(message.author) else "")
            await message.reply(hello, mention_author=False)
            return
        if parsed.intent == Intent.STOP:
            await message.reply(
                "There's nothing running here. To stop a run, say stop in its thread.",
                mention_author=False,
            )
            return
        if await self._handle_admin_phrase(message, text, parsed=parsed):
            return

        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            await message.reply(denied, mention_author=False)
            return
        prompt = await agent_prompt_from_message(message, text=text)
        if not prompt.has_content:
            await message.reply(prompt.feedback or HELLO, mention_author=False)
            return

        repo = await self._pick_repo(message, text, prompt)
        if repo is not None:
            await self._launch(message, prompt, repo)

    async def _pick_repo(
        self, message: discord.Message, text: str, prompt: AgentPrompt
    ) -> str | None:
        """The repo to launch in, or None after telling the user what's needed."""
        bound = await channel_repos(self.bot, message.channel)
        admin = self.bot.policy.is_admin(message.author)
        named = find_repo_in_text(text, bound)
        if named is None and (lead := leading_repo_name(text)):
            # "acme/web fix the header" vs "src/app.py is broken": only a real repo counts.
            try:
                known = await self.bot.cursor.list_repos()
            except Exception:  # noqa: BLE001
                logger.warning("list_repos failed while checking %s", lead, exc_info=True)
                known = []
            named = next((r for r in known if same_repo(r, lead)), None)

        if named:
            matched = match_allowed_repo(named, bound) if bound else None
            if matched:
                return matched
            if not admin:
                await message.reply(
                    f"I can only work on {format_repos(bound)} here." if bound else NOT_SET_UP,
                    mention_author=False,
                )
                return None
            if not bound:
                # First repo an admin names in a fresh channel becomes its default.
                problem = await save_channel_repos(self.bot, message.channel, [named])
                if problem:
                    await message.reply(problem, mention_author=False)
            return named

        if len(bound) == 1:
            return bound[0]
        if bound:
            await self._ask_repo(message, prompt, bound, remember=False)
            return None
        if not admin:
            await message.reply(NOT_SET_UP, mention_author=False)
            return None
        try:
            available = await self.bot.cursor.list_repos()
        except Exception as exc:  # noqa: BLE001
            await message.reply(f"I couldn't load your Cursor repos: {exc}", mention_author=False)
            return None
        if not available:
            await message.reply(
                "I don't see any repos. Connect GitHub in the Cursor dashboard, or name "
                "one (`owner/repo`).",
                mention_author=False,
            )
            return None
        await self._ask_repo(message, prompt, available, remember=True)
        return None

    async def _ask_repo(
        self, message: discord.Message, prompt: AgentPrompt, choices: list[str], *, remember: bool
    ) -> None:
        if len(choices) <= 10:
            question = f"Which repo should I use, {format_repos(choices).replace(' and ', ' or ')}?"
        else:
            sample = ", ".join(f"`{compact_repo(r)}`" for r in choices[:5])
            question = f"Which repo should I use? (e.g. {sample}, …)"
        await message.reply(question, mention_author=False)
        self.repo_questions[(message.channel.id, message.author.id)] = RepoQuestion(
            message=message,
            prompt=prompt,
            choices=choices,
            remember=remember,
            expires_at=time.monotonic() + REPO_QUESTION_TTL_SEC,
        )

    async def _maybe_answer_repo_question(self, message: discord.Message) -> None:
        key = (message.channel.id, message.author.id)
        question = self.repo_questions.get(key)
        if question is None:
            return
        if time.monotonic() > question.expires_at:
            del self.repo_questions[key]
            return
        answer = (message.content or "").strip()
        if not answer or len(answer.split()) > 4:
            return  # ordinary chatter, not an answer
        repo = next(
            filter(None, (match_allowed_repo(c, question.choices) for c in [answer, *answer.split()])),
            None,
        )
        if repo is None:
            if "/" in answer:
                await message.reply(
                    "I don't see that one. Which of these did you mean? "
                    + ", ".join(f"`{compact_repo(r)}`" for r in question.choices[:10]),
                    mention_author=False,
                )
            return
        del self.repo_questions[key]
        denied = self.bot.policy.authorize(user=message.author, guild=message.guild)
        if denied:
            await message.reply(denied, mention_author=False)
            return
        if question.remember:
            problem = await save_channel_repos(self.bot, message.channel, [repo])
            if problem:
                await message.reply(problem, mention_author=False)
        await self._launch(question.message, question.prompt, repo)

    # --- admin phrasing -------------------------------------------------------------

    async def _handle_admin_phrase(
        self, message: discord.Message, text: str, parsed: Parsed | None = None
    ) -> bool:
        """"use owner/repo here", "give @x access", … Returns True if it was one."""
        parsed = parsed or classify(
            text,
            mentions_people=any(m.id != self.me.id for m in message.mentions),
            has_files=bool(message.attachments),
        )
        if parsed.intent not in {Intent.BIND, Intent.UNBIND, Intent.GRANT, Intent.REVOKE}:
            return False
        denied = self.bot.policy.check_guild(message.guild) or self.bot.policy.check_admin(
            message.author
        )
        if denied:
            await message.reply(denied, mention_author=False)
            return True
        if parsed.intent in {Intent.GRANT, Intent.REVOKE}:
            await self._set_access(message, grant=parsed.intent == Intent.GRANT)
        else:
            await self._set_channel_repos(message, parsed.repos, add=parsed.intent == Intent.BIND)
        return True

    async def _set_channel_repos(
        self, message: discord.Message, repos: list[str], *, add: bool
    ) -> None:
        current = await channel_repos(self.bot, message.channel)
        if add:
            updated = current + [r for r in repos if not any(same_repo(r, c) for c in current)]
        else:
            updated = [c for c in current if not any(same_repo(r, c) for r in repos)]
        if updated == current:
            await message.reply(
                f"This channel already works on {format_repos(current)}."
                if current
                else "This channel doesn't have any repos set.",
                mention_author=False,
            )
            return
        problem = await save_channel_repos(self.bot, message.channel, updated)
        if problem:
            await message.reply(problem, mention_author=False)
        elif updated:
            await message.reply(
                f"Got it. This channel works on {format_repos(updated)}.", mention_author=False
            )
        else:
            await message.reply("Got it. This channel has no repos now.", mention_author=False)

    async def _set_access(self, message: discord.Message, *, grant: bool) -> None:
        guild = message.guild
        assert guild is not None
        members = [m for m in message.mentions if isinstance(m, discord.Member) and m.id != self.me.id]
        name = self.bot.settings.cursor_role_name
        # The bot's own integration role usually shares this name and can't be assigned.
        role = next((r for r in guild.roles if r.name == name and not r.managed), None)
        try:
            if grant and role is None:
                role = await guild.create_role(name=name, reason="Cursor bot access role")
            for member in members:
                if grant:
                    await member.add_roles(role, reason=f"granted by {message.author}")
                elif role is not None and role in member.roles:
                    await member.remove_roles(role, reason=f"revoked by {message.author}")
        except discord.Forbidden:
            await message.reply(
                f"I need **Manage Roles**, and my role has to be above **{name}**.",
                mention_author=False,
            )
            return
        who = ", ".join(m.mention for m in members)
        if grant:
            reply = f"Done. {who} can use me now."
        else:
            reply = f"Done. {who} can't use me anymore."
            settings = self.bot.settings
            still = [
                m.mention
                for m in members
                if self.bot.policy.is_admin(m) or m.id in settings.allowed_user_ids
            ]
            if still:
                reply += f" ({', '.join(still)} still have access as admins or allowlisted users.)"
        await message.reply(reply, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # --- running the agent ----------------------------------------------------------

    async def _launch(self, message: discord.Message, prompt: AgentPrompt, repo: str) -> None:
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
        else:
            try:
                thread = await message.create_thread(
                    name=_thread_name(prompt.display), auto_archive_duration=1440
                )
            except discord.HTTPException as exc:
                logger.warning("create_thread failed: %s", exc)
                await message.reply(
                    "I couldn't open a thread here. I need **Create Public Threads**.",
                    mention_author=False,
                )
                return
        # Busy from the start: replies before the agent exists get queued, not dropped.
        state = self.threads[thread.id] = ThreadState(busy=True, repo=repo)
        try:
            status = await thread.send(chat_ui.working(repo))
        except discord.HTTPException:
            state.busy = False
            raise
        await self._converse(thread, state, prompt, status=status, author=message.author)

    async def _converse(
        self,
        thread: discord.abc.Messageable,
        state: ThreadState,
        prompt: AgentPrompt,
        *,
        status: discord.Message,
        author: discord.abc.User | None = None,
    ) -> None:
        """Run a turn, then any messages that arrived meanwhile, until the queue is empty."""
        state.busy = True
        try:
            while True:
                await self._turn(thread, state, prompt, status, author=author)
                if state.pending and state.agent_id is None:
                    state.pending.clear()
                    await thread.send(
                        "Since that didn't start, I didn't get to your other messages "
                        "either. Reply here to try again."
                    )
                    return
                if not state.pending:
                    return
                prompt = merge_agent_prompts(state.pending)
                state.pending.clear()
                status = await thread.send(chat_ui.working())
        finally:
            state.busy = False

    async def _turn(
        self,
        thread: discord.abc.Messageable,
        state: ThreadState,
        prompt: AgentPrompt,
        status_message: discord.Message,
        *,
        author: discord.abc.User | None,
    ) -> None:
        """Follow up with the thread's agent, or (re)launch one in ``state.repo``."""
        status = chat_ui.StatusMessage(status_message)

        async def on_started(agent_id: str) -> None:
            state.agent_id = agent_id

        async def launch() -> TurnResult:
            assert state.repo is not None
            try:
                return await self.bot.cursor.launch(
                    prompt.sdk_message(),
                    repo_url=state.repo,
                    metadata={
                        "source": "discord",
                        "thread_id": str(getattr(thread, "id", "")),
                        "discord_user_id": str(author.id) if author else "",
                    },
                    on_started=on_started,
                    on_activity=status.activity,
                )
            except Exception:
                state.agent_id = None  # never got going: nothing to follow up on
                raise

        try:
            if state.agent_id is None:
                result = await launch()
            else:
                try:
                    result = await self.bot.cursor.follow_up(
                        state.agent_id, prompt.sdk_message(), on_activity=status.activity
                    )
                except Exception as exc:
                    if not is_not_found_error(exc):
                        raise
                    # The agent is gone on Cursor's side. Start fresh in the thread's repo;
                    # threads recovered after a redeploy fall back to the channel's only repo.
                    state.agent_id = None
                    if state.repo is None:
                        bound = await channel_repos(self.bot, thread)
                        state.repo = bound[0] if len(bound) == 1 else None
                    if state.repo is None:
                        await status.finish(
                            "That agent no longer exists on Cursor's side. Mention me here "
                            "with what you need (and which repo) and I'll start a new one."
                        )
                        return
                    result = await launch()
        except Exception as exc:  # noqa: BLE001
            # A run we didn't start (e.g. before a redeploy) is still going: steer it.
            if is_busy_error(exc) and state.agent_id:
                if await self._try_steer(state.agent_id, prompt):
                    await status.finish("I'm still working on the last thing. I'll fold this in.")
                    return
            logger.exception("agent turn failed")
            await status.finish(chat_ui.failed(exc))
            return
        if not result.agent_id:
            state.agent_id = None  # Cursor never kept the agent; a reply retries
        await self._reply(thread, status, result)

    async def _reply(
        self, thread: discord.abc.Messageable, status: chat_ui.StatusMessage, result: TurnResult
    ) -> None:
        chunks = chat_ui.reply_chunks(result)
        view = chat_ui.pr_button(result.pr_url)
        await status.finish(chunks[0], view=view if len(chunks) == 1 else None)
        for i, chunk in enumerate(chunks[1:], start=2):
            if i == len(chunks) and view is not None:
                await thread.send(chunk, view=view)
            else:
                await thread.send(chunk)
        posted = self.posted_artifacts.setdefault(result.agent_id, set())
        try:
            await deliver_new_cloud_artifacts(
                thread=thread, cursor=self.bot.cursor, agent_id=result.agent_id, posted=posted
            )
        except Exception:  # noqa: BLE001
            logger.exception("artifact delivery failed for %s", result.agent_id)


async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


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


def _thread_name(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    return (cleaned[:77] + "…" if len(cleaned) > 80 else cleaned) or "cursor-agent"
