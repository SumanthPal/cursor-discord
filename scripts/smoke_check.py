#!/usr/bin/env python3
"""Offline smoke checks (no Discord / Cursor network required)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.agent_ref import (  # noqa: E402
    chat_done,
    chat_progress,
    chat_working,
    is_agent_id,
    parse_ref_spoiler,
)
from bot.commands import chunk_discord_lines, display_project_alias  # noqa: E402
from bot.policy import Policy  # noqa: E402
from bot.config import Settings  # noqa: E402
from bot.projects import (  # noqa: E402
    load_registry,
    parse_channel_project,
    parse_channel_repo,
    set_channel_project_topic,
    set_channel_repo_topic,
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
    # Example file is commented nicknames-only; empty registry is valid.
    assert isinstance(reg.projects, dict)
    print("ok projects_yaml")


def test_channel_bind() -> None:
    assert parse_channel_project("hello cursor-project:instinct world") == "instinct"
    topic = set_channel_project_topic("general chat", "website")
    assert "cursor-project:website" in topic
    topic2 = set_channel_project_topic(topic, "instinct")
    assert parse_channel_project(topic2) == "instinct"

    repo_topic = set_channel_repo_topic("standup", "https://github.com/acme/widgets")
    assert repo_topic == "standup cursor-repo:acme/widgets"
    assert parse_channel_repo(repo_topic) == "https://github.com/acme/widgets"
    reg = load_registry(projects_json='{"projects":{}}')
    pinned = reg.resolve(channel_topic=repo_topic)
    assert pinned is not None and pinned.alias == "_channel"
    assert pinned.repo.endswith("/acme/widgets")
    assert display_project_alias("_channel") is None
    assert display_project_alias("instinct") == "instinct"
    print("ok channel_bind")


def test_agent_chat() -> None:
    agent_id = "bc-00000000-0000-0000-0000-000000000001"
    assert is_agent_id(agent_id)
    assert not is_agent_id("agent-local")

    done = chat_done(
        agent_id=agent_id,
        status="finished",
        summary="fixed auth",
        run_id="run-1",
        pr_url="https://github.com/you/Instinct-2.0/pull/1",
    )
    spoiler = parse_ref_spoiler(done)
    assert spoiler is not None
    assert spoiler.agent_id == agent_id
    assert spoiler.run_id == "run-1"
    assert "fixed auth" in done
    assert "your turn" in done.lower()
    assert "||cursor-agent:" in done

    working = chat_working(repo="https://github.com/you/Instinct-2.0")
    assert "on it" in working.lower()
    progress = chat_progress(snippet="editing auth.ts", agent_id=agent_id)
    assert "still working" in progress.lower()
    print("ok agent_chat")


def test_chunk_footer() -> None:
    lines = [f"• `owner/repo-{i:04d}`" for i in range(80)]
    pages = chunk_discord_lines(
        "**80 repos**",
        lines,
        footer="use /ask",
        limit=500,
    )
    assert pages
    assert all(len(p) <= 500 for p in pages)
    assert pages[-1].endswith("use /ask")
    # No silent drops: every repo label appears across pages (footer only on last).
    bodies = pages[:-1] + [pages[-1][: -len("\nuse /ask")]]
    joined = "\n".join(bodies)
    for line in lines:
        assert line in joined, line
    print("ok chunk_footer")


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


def test_repo_refs() -> None:
    from bot.repo_refs import decode_repo_ref, encode_repo_ref

    cache: dict[str, str] = {}
    long_url = (
        "https://github.com/some-org/"
        + ("very-long-repo-name-" * 8)
        + "final"
    )
    assert len(long_url) > 100
    key = encode_repo_ref(long_url, cache)
    assert len(key) <= 100
    assert decode_repo_ref(key, cache) == long_url.rstrip("/")

    short = "https://github.com/acme/app"
    key2 = encode_repo_ref(short, cache)
    assert key2 == "acme/app"
    assert decode_repo_ref(key2, cache) == short
    print("ok repo_refs")


def main() -> None:
    test_imports()
    test_projects_json()
    test_projects_yaml_file()
    test_channel_bind()
    test_agent_chat()
    test_chunk_footer()
    test_policy()
    test_repo_refs()
    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
