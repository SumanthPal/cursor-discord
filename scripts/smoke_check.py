#!/usr/bin/env python3
"""Offline smoke checks (no Discord / Cursor network required)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.agent_ref import (  # noqa: E402
    chat_done,
    is_agent_id,
    parse_agent_ref_from_embed,
    parse_ref_spoiler,
)
from bot.turn_coordinator import is_agent_busy_error  # noqa: E402
from bot.multimodal import coalesce_ack  # noqa: E402
from bot.discord_text import chunk_discord_lines  # noqa: E402
from bot.cloud_artifacts import (  # noqa: E402
    artifact_filename,
    artifact_fingerprint,
    is_image_artifact,
)
from bot.multimodal import AgentPrompt, is_image_attachment  # noqa: E402
from cursor_sdk import SdkArtifact  # noqa: E402
from cursor_sdk import SDKImage  # noqa: E402
from bot.policy import Policy  # noqa: E402
from bot.config import Settings  # noqa: E402
from bot.projects import (  # noqa: E402
    load_registry,
    clear_channel_repo_topic,
    match_allowed_repo,
    parse_channel_project,
    parse_channel_repos,
    same_repo,
    set_channel_project_topic,
    set_channel_repo_topic,
    split_repo_prefix,
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
    assert parse_channel_repos(repo_topic) == ["https://github.com/acme/widgets"]
    reg = load_registry(projects_json='{"projects":{"web":{"repo":"acme/web"}}}')
    pinned = reg.channel_projects(repo_topic)
    assert len(pinned) == 1 and pinned[0].alias == "_channel"
    assert pinned[0].repo.endswith("/acme/widgets")

    multi = set_channel_repo_topic(repo_topic, ["acme/a", "https://github.com/acme/b"])
    assert multi == "standup cursor-repo:acme/a,acme/b"
    assert [p.repo for p in reg.channel_projects(multi)] == [
        "https://github.com/acme/a",
        "https://github.com/acme/b",
    ]
    assert clear_channel_repo_topic(multi) == "standup"
    assert reg.channel_projects("cursor-project:web")[0].repo.endswith("/acme/web")
    assert reg.channel_projects("no binding") == []
    assert same_repo("acme/A", "https://github.com/acme/a/")
    print("ok channel_bind")


def test_split_repo_prefix() -> None:
    url, rest = split_repo_prefix("SumanthPal/cursor-discord fix flaky test")
    assert url == "https://github.com/SumanthPal/cursor-discord"
    assert rest == "fix flaky test"
    url2, rest2 = split_repo_prefix(
        "https://github.com/acme/app.git please ship it"
    )
    assert url2 == "https://github.com/acme/app"
    assert rest2 == "please ship it"
    url3, rest3 = split_repo_prefix("just a normal prompt")
    assert url3 is None and rest3 == "just a normal prompt"
    assert split_repo_prefix("acme/app") == (
        "https://github.com/acme/app",
        "",
    )

    allowed = [
        "https://github.com/SumanthPal/cursor-discord",
        "https://github.com/acme/widgets",
    ]
    assert (
        match_allowed_repo("SumanthPal/cursor-discord", allowed) == allowed[0]
    )
    assert match_allowed_repo("widgets", allowed) == allowed[1]
    assert match_allowed_repo("nope", allowed) is None
    print("ok split_repo_prefix")


def test_agent_embed() -> None:
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
    assert f"-# `{agent_id}`" in done
    # Legacy spoilers still resolve for older thread messages.
    legacy = parse_ref_spoiler(f"done.\n||cursor-agent:{agent_id} · run:run-9||")
    assert legacy is not None and legacy.agent_id == agent_id and legacy.run_id == "run-9"

    embed = discord.Embed(description="legacy status card")
    embed.set_footer(text=f"cursor-agent:{agent_id} · run:run-1")
    ref = parse_agent_ref_from_embed(embed)
    assert ref is not None
    assert ref.agent_id == agent_id
    assert ref.run_id == "run-1"
    assert embed.footer and embed.footer.text.startswith("cursor-agent:")
    print("ok agent_embed")


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

    def make(**overrides: object) -> Settings:
        base: dict[str, object] = dict(
            discord_token="x",
            discord_client_id="y",
            cursor_api_key="z",
            cursor_model="composer-2.5",
            projects_json=None,
            projects_path="/app/projects.yaml",
            allowed_user_ids=frozenset({1}),
            allowed_guild_ids=frozenset({99}),
            superuser_ids=frozenset(),
            cursor_role_name="Cursor",
            discord_guild_id=None,
            port=8080,
            rate_limit_max=2,
            rate_limit_window_sec=60,
            bridge_workspace="/tmp/x",
        )
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    policy = Policy(make())
    assert policy.authorize(user=U(), guild=G()) is None
    assert policy.authorize(user=U(), guild=G()) is None
    assert policy.authorize(user=U(), guild=G()) is not None  # rate limit

    class BadU:
        id = 2

    assert policy.authorize(user=BadU(), guild=G(), consume_rate=False) is not None
    assert not policy.is_admin(U())

    class Role:
        name = "Cursor"

    class Granted:
        id = 3
        roles = [Role()]

    class Super:
        id = 7

    gated = Policy(make(allowed_user_ids=frozenset(), superuser_ids=frozenset({7})))
    assert gated.check_user(Granted()) is None
    assert gated.check_user(BadU()) is not None
    assert gated.check_user(Super()) is None
    assert gated.is_admin(Super()) and not gated.is_admin(Granted())
    assert gated.check_admin(Granted()) is not None

    open_policy = Policy(make(allowed_user_ids=frozenset()))
    assert open_policy.open_mode and open_policy.is_admin(BadU())
    print("ok policy")


def test_imports() -> None:
    import bot.main  # noqa: F401
    import bot.cursor_agent  # noqa: F401
    import bot.commands  # noqa: F401
    print("ok imports")


def test_cloud_artifacts() -> None:
    art = SdkArtifact(
        path="artifacts/demo/recording_demo.mp4",
        size_bytes=100,
        updated_at="2026-01-01T00:00:00Z",
    )
    fp = artifact_fingerprint(art)
    assert "artifacts/demo" in fp
    assert artifact_filename(art.path) == "recording_demo.mp4"
    assert is_image_artifact("artifacts/screenshot.png")
    assert not is_image_artifact("artifacts/log.txt")
    print("ok cloud_artifacts")


def test_multimodal_prompt() -> None:
    plain = AgentPrompt.from_text("fix the button")
    assert plain.sdk_message() == "fix the button"
    assert plain.display == "fix the button"
    rejected = AgentPrompt(text="", actionable=False, feedback="SVG not supported")
    assert not rejected.has_content
    assert rejected.display == "SVG not supported"
    img = SDKImage.from_data(b"\x89PNG\r\n", "image/png")
    rich = AgentPrompt(text="look at this", images=(img,))
    assert rich.display.endswith("(+1 image)")
    msg = rich.sdk_message()
    assert getattr(msg, "text", None) == "look at this"
    assert getattr(msg, "images", None)

    class FakeAtt:
        content_type = "image/png"
        filename = "x.png"

    assert is_image_attachment(FakeAtt())  # type: ignore[arg-type]
    print("ok multimodal_prompt")


def test_thread_agent_ref() -> None:
    from bot.agent_ref import AgentRef, thread_agent_ref

    agent_id = "bc-00000000-0000-0000-0000-000000000001"
    mapping = {
        42: AgentRef(agent_id=agent_id, run_id="run-live", repo="https://github.com/acme/app")
    }
    ref = thread_agent_ref(42, mapping)
    assert ref is not None and ref.agent_id == agent_id and ref.run_id == "run-live"
    assert thread_agent_ref(99, mapping) is None
    assert thread_agent_ref(42, None) is None
    print("ok thread_agent_ref")


def test_chat_status_trailer() -> None:
    from bot.chat_ui import chat_status_body

    agent_id = "bc-00000000-0000-0000-0000-000000000001"
    working = chat_status_body(
        agent_id=agent_id,
        status="running",
        repo="https://github.com/acme/app",
        run_id="run-1",
    )
    assert agent_id not in working
    assert "run-1" not in working
    assert "working" in working

    body = chat_status_body(
        agent_id=agent_id,
        status="finished",
        summary="x" * 1800,
        run_id="run-1",
        pr_url="https://github.com/acme/app/pull/1",
    )
    assert agent_id in body
    assert "run-1" in body
    print("ok chat_status_trailer")


def test_follow_up_queue() -> None:
    assert is_agent_busy_error(
        Exception("failed_precondition: [agent_busy] Agent already has an active run")
    )
    assert not is_agent_busy_error(Exception("network timeout"))
    assert "turn" in coalesce_ack(merged_count=1).lower()
    assert "3" in coalesce_ack(merged_count=3)
    print("ok follow_up_queue")


def test_cloud_steer_payload() -> None:
    from bot.cloud_steer import prompt_to_steer_payload, steer_ack
    from bot.cloud_steer import SteerOutcome

    assert prompt_to_steer_payload("hi") == {"text": "hi"}
    assert "redirect" in steer_ack(SteerOutcome("steered")).lower()
    print("ok cloud_steer_payload")


def test_cursor_parse() -> None:
    from bot.cursor_parse import MentionKind, parse_mention, strip_inline_options

    parsed = parse_mention("help")
    assert parsed.kind == MentionKind.HELP
    assert parse_mention("").kind == MentionKind.PROMPT
    assert parse_mention("settings.json is broken").kind == MentionKind.PROMPT
    assert parse_mention("settings repos:acme/a").kind == MentionKind.SETTINGS
    parsed2 = parse_mention('repo=acme/app branch=dev autopr=false fix ci')
    assert parsed2.options.repo.endswith("/acme/app")
    assert parsed2.options.branch == "dev"
    assert parsed2.options.auto_create_pr is False
    assert parsed2.prompt == "fix ci"
    parsed3 = parse_mention("agent try a new approach")
    assert parsed3.kind == MentionKind.FORCE_AGENT
    opts, rest = strip_inline_options('model="composer-2.5" ship it')
    assert opts.model == "composer-2.5"
    assert rest == "ship it"
    print("ok cursor_parse")


def test_chat_repo_reply() -> None:
    urls = [
        "https://github.com/acme/widgets",
        "https://github.com/acme/monorepo",
    ]
    assert match_allowed_repo("acme/widgets", urls) == urls[0]
    assert match_allowed_repo("https://github.com/acme/monorepo", urls) == urls[1]
    assert match_allowed_repo("widgets", urls) == urls[0]
    assert match_allowed_repo("nope", urls) is None
    print("ok chat_repo_reply")


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


def test_activity_snippet() -> None:
    from types import SimpleNamespace

    from bot.cursor_agent import _activity_snippet

    tool = SimpleNamespace(
        type="tool_call",
        name="read_file",
        status="running",
        args={"path": "bot/commands.py"},
    )
    assert "`read file`" in _activity_snippet(tool)
    assert "`bot/commands.py`" in _activity_snippet(tool)
    assert _activity_snippet(SimpleNamespace(type="thinking", text="…")) == "thinking…"
    assert (
        _activity_snippet(
            SimpleNamespace(type="status", message="cloning repo", status="running")
        )
        == "cloning repo"
    )
    # Completed tools stay quiet so Discord edits don't flicker.
    assert (
        _activity_snippet(
            SimpleNamespace(type="tool_call", name="shell", status="completed", args={})
        )
        == ""
    )
    print("ok activity_snippet")


def main() -> None:
    test_imports()
    test_projects_json()
    test_projects_yaml_file()
    test_channel_bind()
    test_split_repo_prefix()
    test_agent_embed()
    test_chunk_footer()
    test_policy()
    test_multimodal_prompt()
    test_cloud_artifacts()
    test_thread_agent_ref()
    test_chat_status_trailer()
    test_follow_up_queue()
    test_cloud_steer_payload()
    test_cursor_parse()
    test_chat_repo_reply()
    test_repo_refs()
    test_activity_snippet()
    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
