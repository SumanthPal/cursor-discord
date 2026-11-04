from __future__ import annotations

import time
from collections import defaultdict, deque

import discord

from bot.config import Settings


class Policy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check_guild(self, guild: discord.Guild | None) -> str | None:
        allowed = self.settings.allowed_guild_ids
        if not allowed:
            return None
        if guild is None:
            return "This bot only runs inside allowlisted servers."
        if guild.id not in allowed:
            return "This server is not allowlisted for the Cursor bot."
        return None

    @property
    def open_mode(self) -> bool:
        """No superusers or allowlist configured → anyone in allowed guilds is admin."""
        return not self.settings.superuser_ids and not self.settings.allowed_user_ids

    def is_admin(self, user: discord.abc.User) -> bool:
        return self.open_mode or user.id in self.settings.superuser_ids

    def has_grant(self, user: discord.abc.User) -> bool:
        name = self.settings.cursor_role_name
        return any(r.name == name for r in getattr(user, "roles", ()))

    def check_user(self, user: discord.abc.User) -> str | None:
        if (
            self.is_admin(user)
            or user.id in self.settings.allowed_user_ids
            or self.has_grant(user)
        ):
            return None
        return "You don't have access to Cursor here. Ask an admin to `/grant` you."

    def check_admin(self, user: discord.abc.User) -> str | None:
        if self.is_admin(user):
            return None
        return "Only Cursor admins (`SUPERUSER_IDS`) can do that."

    def check_rate(self, user: discord.abc.User) -> str | None:
        now = time.monotonic()
        window = self.settings.rate_limit_window_sec
        q = self._hits[user.id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= self.settings.rate_limit_max:
            return (
                f"Rate limit: max {self.settings.rate_limit_max} "
                f"actions / {int(window)}s. Try again shortly."
            )
        q.append(now)
        return None

    def authorize(
        self,
        *,
        user: discord.abc.User,
        guild: discord.Guild | None,
        consume_rate: bool = True,
    ) -> str | None:
        err = self.check_guild(guild)
        if err:
            return err
        err = self.check_user(user)
        if err:
            return err
        if consume_rate:
            return self.check_rate(user)
        return None
