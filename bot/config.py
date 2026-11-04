from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _csv_ids(raw: str | None) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_client_id: str
    cursor_api_key: str
    cursor_model: str
    projects_json: str | None
    projects_path: str
    allowed_user_ids: frozenset[int]
    allowed_guild_ids: frozenset[int]
    superuser_ids: frozenset[int]
    cursor_role_name: str
    discord_guild_id: int | None
    port: int
    rate_limit_max: int
    rate_limit_window_sec: float
    bridge_workspace: str


def load_settings() -> Settings:
    load_dotenv()

    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
    discord_client_id = os.environ.get("DISCORD_CLIENT_ID", "").strip()
    cursor_api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not discord_token:
        raise SystemExit("DISCORD_TOKEN is required")
    if not discord_client_id:
        raise SystemExit("DISCORD_CLIENT_ID is required")
    if not cursor_api_key:
        raise SystemExit("CURSOR_API_KEY is required")

    guild_raw = os.environ.get("DISCORD_GUILD_ID", "").strip()
    discord_guild_id = int(guild_raw) if guild_raw else None

    return Settings(
        discord_token=discord_token,
        discord_client_id=discord_client_id,
        cursor_api_key=cursor_api_key,
        cursor_model=os.environ.get("CURSOR_MODEL", "composer-2.5").strip()
        or "composer-2.5",
        projects_json=os.environ.get("PROJECTS_JSON"),
        projects_path=os.environ.get("PROJECTS_PATH", "/app/projects.yaml"),
        allowed_user_ids=_csv_ids(os.environ.get("ALLOWED_USER_IDS")),
        allowed_guild_ids=_csv_ids(os.environ.get("ALLOWED_GUILD_IDS")),
        superuser_ids=_csv_ids(os.environ.get("SUPERUSER_IDS")),
        cursor_role_name=os.environ.get("CURSOR_ROLE_NAME", "Cursor").strip()
        or "Cursor",
        discord_guild_id=discord_guild_id,
        port=int(os.environ.get("PORT", "8080")),
        rate_limit_max=int(os.environ.get("RATE_LIMIT_MAX", "8")),
        rate_limit_window_sec=float(os.environ.get("RATE_LIMIT_WINDOW_SEC", "60")),
        bridge_workspace=os.environ.get(
            "BRIDGE_WORKSPACE", "/tmp/cursor-discord-bridge"
        ),
    )
