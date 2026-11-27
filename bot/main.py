from __future__ import annotations

import logging
import os

import discord
from aiohttp import web
from discord.ext import commands

from bot.commands import AgentCog
from bot.config import Settings, load_settings
from bot.cursor_agent import CursorAgentService
from bot.policy import Policy

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cursor-discord")


class CursorDiscordBot(commands.Bot):
    def __init__(self, *, settings: Settings, policy: Policy, cursor: CursorAgentService) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        # Conversation only: no prefix or slash commands.
        super().__init__(command_prefix=commands.when_mentioned, help_command=None, intents=intents)
        self.settings = settings
        self.policy = policy
        self.cursor = cursor
        self._http_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(AgentCog(self))
        await self.cursor.start()
        await self._start_health_server()
        # Syncing the empty command tree removes slash commands from older versions.
        await self.tree.sync()
        if self.settings.discord_guild_id:
            await self.tree.sync(guild=discord.Object(id=self.settings.discord_guild_id))

    async def process_commands(self, message: discord.Message) -> None:
        return  # every message is handled by AgentCog.on_message

    async def close(self) -> None:
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None
        await self.cursor.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user and self.user.id)

    async def _start_health_server(self) -> None:
        async def healthz(_: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/", healthz)
        app.router.add_get("/healthz", healthz)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.settings.port).start()
        self._http_runner = runner
        logger.info("health server on :%s", self.settings.port)


def build_bot(settings: Settings | None = None) -> CursorDiscordBot:
    settings = settings or load_settings()
    cursor = CursorAgentService(
        api_key=settings.cursor_api_key,
        model=settings.cursor_model,
        bridge_workspace=settings.bridge_workspace,
    )
    return CursorDiscordBot(settings=settings, policy=Policy(settings), cursor=cursor)


def main() -> None:
    bot = build_bot()
    try:
        bot.run(bot.settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
