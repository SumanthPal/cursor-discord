#!/usr/bin/env python3
"""Offline smoke checks (no Discord / Cursor network required)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.agent_ref import (  # noqa: E402
    build_starter_embed,
    is_agent_id,
    parse_agent_ref_from_embed,
)
from bot.policy import Policy  # noqa: E402
from bot.config import Settings  # noqa: E402
from bot.projects import (  # noqa: E402
    load_registry,
    parse_channel_project,
    set_channel_project_topic,
)


def test_projects_json() -> None:
    raw = json.dumps(
        {
            "default": "instinct",
            "projects": {
                "instinct": {
                    "repo": "https://github.com/you/Instinct-2.0",
                    "branch": "main",
                },
                "website": {"repo": "you/website", "branch": "dev"},
            },
        }
    )
    reg = load_registry(projects_json=raw)
    assert reg.default == "instinct"
    assert reg.get("website").repo == "https://github.com/you/website"
    resolved = reg.resolve(project="website")
    assert resolved is not None and resolved.branch == "dev"
    explicit = reg.resolve(repo="acme/app", branch="feat")
    assert explicit is not None and explicit.repo.endswith("/acme/app")
    print("ok projects_json")


def test_projects_yaml_file() -> None:
    example = ROOT / "projects.example.yaml"
    reg = load_registry(projects_path=example)
    assert "instinct" in reg.projects
    print("ok projects_yaml")


def test_channel_bind() -> None:
    assert parse_channel_project("hello cursor-project:instinct world") == "instinct"
    topic = set_channel_project_topic("general chat", "website")
    assert "cursor-project:website" in topic
    topic2 = set_channel_project_topic(topic, "instinct")
    assert parse_channel_project(topic2) == "instinct"
    print("ok channel_bind")


def test_agent_embed() -> None:
    assert is_agent_id("bc-00000000-0000-0000-0000-000000000001")
    assert not is_agent_id("agent-local")
    embed = build_starter_embed(
        agent_id="bc-00000000-0000-0000-0000-000000000001",
        prompt="fix auth",
        project="instinct",
        repo="https://github.com/you/Instinct-2.0",
        status="running",
        run_id="run-1",
    )
    ref = parse_agent_ref_from_embed(embed)
    assert ref is not None
    assert ref.agent_id.startswith("bc-")
    assert ref.project == "instinct"
    assert embed.title  # human status title
    print("ok agent_embed")


def test_policy() -> None:
    class U:
        id = 1

    class G:
        id = 99

    settings = Settings(
        discord_token="x",
        discord_client_id="y",
        cursor_api_key="z",
        cursor_model="composer-2.5",
        projects_json=None,
        projects_path="/app/projects.yaml",
        allowed_user_ids=frozenset({1}),
        allowed_guild_ids=frozenset({99}),
        discord_guild_id=None,
        port=8080,
        rate_limit_max=2,
        rate_limit_window_sec=60,
        bridge_workspace="/tmp/x",
    )
    policy = Policy(settings)
    assert policy.authorize(user=U(), guild=G()) is None
    assert policy.authorize(user=U(), guild=G()) is None
    assert policy.authorize(user=U(), guild=G()) is not None  # rate limit

    class BadU:
        id = 2

    assert policy.authorize(user=BadU(), guild=G(), consume_rate=False) is not None
    print("ok policy")


def test_imports() -> None:
    import bot.main  # noqa: F401
    import bot.cursor_agent  # noqa: F401
    import bot.commands  # noqa: F401
    print("ok imports")


def main() -> None:
    test_imports()
    test_projects_json()
    test_projects_yaml_file()
    test_channel_bind()
    test_agent_embed()
    test_policy()
    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
