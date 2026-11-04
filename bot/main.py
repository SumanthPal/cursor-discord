from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from aiohttp import web
from discord.ext import commands

from bot.commands import AgentCog
from bot.config import Settings, load_settings
from bot.cursor_agent import CursorAgentService, ensure_api_key_env
from bot.policy import Policy
from bot.projects import ProjectRegistry, load_registry

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cursor-discord")


class CursorDiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ProjectRegistry,
        policy: Policy,
        cursor: CursorAgentService,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.registry = registry
        self.policy = policy
        self.cursor = cursor
        # Short Discord choice values → full repo URLs (autocomplete).
        self.repo_ref_cache: dict[str, str] = {}
        # Cloud agent artifact paths already posted to Discord (per bc- id).
        self.posted_artifacts: dict[str, set[str]] = {}
        self._http_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(AgentCog(self))
        ensure_api_key_env(self.settings.cursor_api_key)
        await self.cursor.start()
        await self._start_health_server()

        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %s guild commands to %s",
                len(synced),
                self.settings.discord_guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global commands", len(synced))

    async def close(self) -> None:
        await self._stop_health_server()
        await self.cursor.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user and self.user.id)

    async def _start_health_server(self) -> None:
        app = web.Application()

        async def healthz(_: web.Request) -> web.Response:
            return web.Response(text="ok")

        app.router.add_get("/", healthz)
        app.router.add_get("/healthz", healthz)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.port)
        await site.start()
        self._http_runner = runner
        logger.info("health server on :%s", self.settings.port)

    async def _stop_health_server(self) -> None:
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None


def build_bot(settings: Settings | None = None) -> CursorDiscordBot:
    settings = settings or load_settings()
    # Prefer local projects.yaml when developing outside the container
    projects_path = settings.projects_path
    local_fallback = Path("projects.yaml")
    if not Path(projects_path).is_file() and local_fallback.is_file():
        projects_path = str(local_fallback)
    example = Path("projects.example.yaml")
    if (
        not settings.projects_json
        and not Path(projects_path).is_file()
        and example.is_file()
    ):
        projects_path = str(example)

    registry = load_registry(
        projects_json=settings.projects_json,
        projects_path=projects_path,
    )
    logger.info(
        "Loaded %s projects (default=%s)",
        len(registry.projects),
        registry.default,
    )
    app_id = settings.discord_client_id
    logger.info(
        "Discord app id %s — invite: https://discord.com/oauth2/authorize?client_id=%s&permissions=67648&scope=bot%%20applications.commands",
        app_id,
        app_id,
    )
    policy = Policy(settings)
    cursor = CursorAgentService(
        api_key=settings.cursor_api_key,
        default_model=settings.cursor_model,
        bridge_workspace=settings.bridge_workspace,
    )
    return CursorDiscordBot(
        settings=settings, registry=registry, policy=policy, cursor=cursor
    )


def main() -> None:
    bot = build_bot()
    try:
        bot.run(bot.settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
