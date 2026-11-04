from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
import discord

from bot.agent_ref import is_agent_id
from bot.channel_bind import pin_channel_repo
from bot.multimodal import AgentPrompt
from bot.projects import Project
from bot.repo_refs import DISCORD_VALUE_MAX, repo_path, repos_matching

if TYPE_CHECKING:
    from bot.main import CursorDiscordBot

logger = logging.getLogger(__name__)

OnRepoPicked = Callable[[discord.Interaction, Project], Awaitable[None]]

# Discord select menus allow at most 25 options.
SELECT_PAGE_SIZE = 25

# Sentinel so rebuild(search_query=None) clears filter; omit arg to keep.
_UNSET: Any = object()


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


def _truncate_prompt(prompt: str, *, limit: int = 200) -> str:
    text = (prompt or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class RepoSearchModal(discord.ui.Modal, title="Search repositories"):
    """Filter the repo list by owner, name, or URL fragment."""

    def __init__(self, picker: RepoPickerView) -> None:
        super().__init__()
        self._picker = picker
        self._query = discord.ui.TextInput(
            label="Search",
            placeholder="e.g. acme, frontend, org/my-app",
            default=picker.search_query or "",
            max_length=100,
            required=False,
        )
        self.add_item(self._query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        denied = self._picker._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        query = (self._query.value or "").strip()
        matches = repos_matching(self._picker.all_urls, query)
        if not matches:
            await interaction.response.send_message(
                f"No repos match **{query or '(empty)'}**. Try a shorter fragment "
                "or check `/repos`.",
                ephemeral=True,
            )
            return

        # Always filter the picker — never auto-pin on a single match.
        new_view = self._picker.rebuild(
            search_query=query or None,
            page=0,
        )
        await interaction.response.edit_message(
            content=new_view.message_content(),
            view=new_view,
        )


class RepoPickerView(discord.ui.View):
    """Pick a Cursor-connected repo. Search + pages when the list is long."""

    def __init__(
        self,
        bot: CursorDiscordBot,
        *,
        prompt: AgentPrompt,
        repo_urls: list[str],
        branch: str | None,
        on_picked: OnRepoPicked,
        owner_id: int,
        channel: discord.abc.Messageable | None = None,
        page: int = 0,
        search_query: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.prompt = prompt
        self.branch = branch
        self.on_picked = on_picked
        self.owner_id = owner_id
        # Keep raw handle — may be PartialMessageable; resolve at pin time.
        self.channel = channel
        self.all_urls = [u.rstrip("/") for u in repo_urls]
        self.page = max(0, page)
        self.search_query = (search_query or "").strip() or None

        self._visible_urls = self._page_slice()
        if self._visible_urls:
            select = discord.ui.Select(
                placeholder=self._select_placeholder(),
                options=self._select_options(),
                min_values=1,
                max_values=1,
            )
            select.callback = self._picked  # type: ignore[method-assign]
            self.add_item(select)

        search = discord.ui.Button(
            label="Search",
            style=discord.ButtonStyle.primary,
            emoji="🔎",
        )
        search.callback = self._open_search  # type: ignore[method-assign]
        self.add_item(search)

        filtered = self._filtered_urls()
        page_count = self._page_count(filtered)
        if page_count > 1:
            prev = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                disabled=self.page <= 0,
            )
            prev.callback = self._prev_page  # type: ignore[method-assign]
            self.add_item(prev)

            next_btn = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= page_count - 1,
            )
            next_btn.callback = self._next_page  # type: ignore[method-assign]
            self.add_item(next_btn)

        if self.search_query:
            clear = discord.ui.Button(
                label="Clear search",
                style=discord.ButtonStyle.secondary,
            )
            clear.callback = self._clear_search  # type: ignore[method-assign]
            self.add_item(clear)

    def rebuild(
        self,
        *,
        page: int | None = None,
        search_query: Any = _UNSET,
    ) -> RepoPickerView:
        if search_query is _UNSET:
            next_query = self.search_query
        else:
            next_query = search_query
        return RepoPickerView(
            self.bot,
            prompt=self.prompt,
            repo_urls=self.all_urls,
            branch=self.branch,
            on_picked=self.on_picked,
            owner_id=self.owner_id,
            channel=self.channel,
            page=self.page if page is None else page,
            search_query=next_query,
        )

    def message_content(self) -> str:
        filtered = self._filtered_urls()
        total = len(self.all_urls)
        shown = len(filtered)
        lines = [f"Pick a repo for: **{_truncate_prompt(self.prompt.display)}**"]
        if self.search_query:
            lines.append(
                f"_Filter `{self.search_query}` — {shown} match"
                f"{'es' if shown != 1 else ''}_"
            )
        elif total > SELECT_PAGE_SIZE:
            page_count = self._page_count(filtered)
            lines.append(
                f"_Showing page {self.page + 1}/{page_count} "
                f"({total} repos — use **Search** or **Next**)_"
            )
        elif total > 10:
            lines.append("_Use **Search** to filter the list._")
        return "\n".join(lines)

    def _filtered_urls(self) -> list[str]:
        return repos_matching(self.all_urls, self.search_query or "")

    def _page_count(self, filtered: list[str]) -> int:
        if not filtered:
            return 1
        return max(1, math.ceil(len(filtered) / SELECT_PAGE_SIZE))

    def _page_slice(self) -> list[str]:
        filtered = self._filtered_urls()
        page_count = self._page_count(filtered)
        self.page = min(self.page, page_count - 1)
        start = self.page * SELECT_PAGE_SIZE
        return filtered[start : start + SELECT_PAGE_SIZE]

    def _select_placeholder(self) -> str:
        filtered = self._filtered_urls()
        if len(filtered) <= SELECT_PAGE_SIZE:
            return "Choose a repo…"
        return f"Choose a repo… (page {self.page + 1}/{self._page_count(filtered)})"

    def _select_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for i, url in enumerate(self._visible_urls):
            options.append(
                discord.SelectOption(
                    label=short_repo_label(url),
                    value=str(i),
                    description=_desc(url),
                )
            )
        return options

    def _deny_if_not_owner(self, interaction: discord.Interaction) -> str | None:
        if interaction.user.id != self.owner_id:
            return "Only the person who started this can pick a repo."
        return self.bot.policy.authorize(
            user=interaction.user, guild=interaction.guild
        )

    async def _open_search(self, interaction: discord.Interaction) -> None:
        denied = self._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.send_modal(RepoSearchModal(self))

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        denied = self._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        new_view = self.rebuild(page=max(0, self.page - 1))
        await interaction.response.edit_message(
            content=new_view.message_content(),
            view=new_view,
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        denied = self._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        new_view = self.rebuild(page=self.page + 1)
        await interaction.response.edit_message(
            content=new_view.message_content(),
            view=new_view,
        )

    async def _clear_search(self, interaction: discord.Interaction) -> None:
        denied = self._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        new_view = self.rebuild(search_query=None, page=0)
        await interaction.response.edit_message(
            content=new_view.message_content(),
            view=new_view,
        )

    async def _picked(self, interaction: discord.Interaction) -> None:
        denied = self._deny_if_not_owner(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        select = interaction.data.get("values", []) if interaction.data else []
        if not select:
            await interaction.response.send_message(
                "Pick a repo from the menu.", ephemeral=True
            )
            return
        try:
            url = self._visible_urls[int(select[0])]
        except (ValueError, IndexError) as exc:
            await interaction.response.send_message(
                f"Invalid repo selection: {exc}", ephemeral=True
            )
            return

        # Ack first — topic edit can exceed Discord's 3s interaction window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        pin_note = await pin_channel_repo(self.bot, self.channel, url)
        await interaction.followup.send(pin_note, ephemeral=True)

        project = Project(
            alias="_explicit",
            repo=url,
            branch=(self.branch or "").strip() or None,
            model=self.bot.settings.cursor_model,
        )
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
