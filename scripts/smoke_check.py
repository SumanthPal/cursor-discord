#!/usr/bin/env python3
"""Offline smoke checks (no Discord / Cursor network required)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cursor_sdk import SDKImage, SdkArtifact  # noqa: E402

from bot import chat_ui  # noqa: E402
from bot.agent_ref import is_agent_id, parse_ref, ref_trailer  # noqa: E402
from bot.cloud_artifacts import (  # noqa: E402
    artifact_filename,
    artifact_fingerprint,
    is_image_artifact,
)
from bot.config import DEFAULT_MODEL, Settings  # noqa: E402
from bot.cursor_agent import TurnResult, activity_line, is_busy_error  # noqa: E402
from bot.cursor_parse import Intent, classify, is_stop, strip_bot_mention  # noqa: E402
from bot.discord_text import split_discord  # noqa: E402
from bot.multimodal import AgentPrompt, is_image_attachment, merge_agent_prompts  # noqa: E402
from bot.policy import Policy  # noqa: E402
from bot.repos import (  # noqa: E402
    compact_repo,
    find_repo_in_text,
    format_repos,
    match_allowed_repo,
    normalize_repo,
    parse_channel_repos,
    repo_tokens,
    same_repo,
    set_channel_repo_topic,
)

A = "https://github.com/acme/web"
B = "https://github.com/acme/api"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        discord_token="x",
        discord_client_id="y",
        cursor_api_key="z",
        cursor_model=DEFAULT_MODEL,
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


def test_imports() -> None:
    import bot.commands  # noqa: F401
    import bot.main  # noqa: F401

    assert DEFAULT_MODEL == "grok-4.7"
    print("ok imports")


def test_policy() -> None:
    user = SimpleNamespace(id=1)
    guild = SimpleNamespace(id=99)
    policy = Policy(make_settings())
    assert policy.authorize(user=user, guild=guild) is None
    assert policy.authorize(user=user, guild=guild) is None
    assert policy.authorize(user=user, guild=guild) is not None  # rate limit

    stranger = SimpleNamespace(id=2)
    assert policy.authorize(user=stranger, guild=guild, consume_rate=False) is not None
    assert not policy.is_admin(user)

    granted = SimpleNamespace(id=3, roles=[SimpleNamespace(name="Cursor", managed=False)])
    admin = SimpleNamespace(id=7)
    gated = Policy(make_settings(allowed_user_ids=frozenset(), superuser_ids=frozenset({7})))
    assert gated.check_user(granted) is None
    assert gated.check_user(stranger) is not None
    assert gated.check_user(admin) is None
    assert gated.is_admin(admin) and not gated.is_admin(granted)
    assert gated.check_admin(granted) is not None

    open_policy = Policy(make_settings(allowed_user_ids=frozenset()))
    assert open_policy.open_mode and open_policy.is_admin(stranger)
    print("ok policy")


def test_repos() -> None:
    assert normalize_repo("acme/web") == A
    assert normalize_repo("https://github.com/acme/web.git/") == A
    assert compact_repo(A) == "acme/web"
    assert same_repo("acme/WEB", A)

    topic = set_channel_repo_topic("Frontend work", [A, B])
    assert topic == "Frontend work cursor-repo:acme/web,acme/api"
    assert parse_channel_repos(topic) == [A, B]
    assert parse_channel_repos(set_channel_repo_topic(topic, [B])) == [B]
    assert set_channel_repo_topic(topic, []) == "Frontend work"
    assert parse_channel_repos("no binding") == []

    assert find_repo_in_text("acme/web fix the header") == A
    assert find_repo_in_text("in https://github.com/acme/api, add caching") == B
    assert find_repo_in_text("in acme/api add caching", [A, B]) == B
    # A file path mid-sentence is not a repo.
    assert find_repo_in_text("fix the bug in src/app.py", [A, B]) is None
    assert find_repo_in_text("fix the login test") is None

    assert match_allowed_repo("api", [A, B]) == B
    assert match_allowed_repo("acme", [A, B]) is None  # ambiguous
    assert match_allowed_repo("`acme/web`", [A, B]) == A

    assert repo_tokens("use acme/web and https://github.com/acme/api here") == [A, B]
    assert format_repos([A]) == "`acme/web`"
    assert format_repos([A, B]) == "`acme/web` and `acme/api`"
    print("ok repos")


def test_intents() -> None:
    def intent(text: str, **kw: bool) -> Intent:
        return classify(text, **kw).intent

    assert intent("") == Intent.HELLO
    assert intent("hey") == Intent.HELLO
    assert intent("what can you do?") == Intent.HELLO
    assert intent("", has_files=True) == Intent.TASK  # screenshot only

    assert intent("stop") == Intent.STOP
    assert intent("Never mind!") == Intent.STOP
    assert is_stop("<@42> stop")
    assert not is_stop("stop the server from crashing on boot")

    assert classify("use acme/web here").repos == [A]
    assert intent("use acme/web here") == Intent.BIND
    assert intent("acme/web") == Intent.BIND
    assert intent("work on acme/web and acme/api in this channel") == Intent.BIND
    assert intent("stop using acme/web here") == Intent.UNBIND
    assert intent("acme/web fix the header") == Intent.TASK
    assert intent("use acme/web to add dark mode") == Intent.TASK

    assert intent("give <@5> access", mentions_people=True) == Intent.GRANT
    assert intent("grant <@5>", mentions_people=True) == Intent.GRANT
    assert intent("let <@5> and <@6> use you", mentions_people=True) == Intent.GRANT
    assert intent("remove <@5>'s access", mentions_people=True) == Intent.REVOKE
    assert intent("revoke <@5>", mentions_people=True) == Intent.REVOKE
    # Mentioning someone in an actual task stays a task.
    assert intent("give <@5>'s PR a review", mentions_people=True) == Intent.TASK
    assert intent("give access", mentions_people=False) == Intent.TASK

    assert strip_bot_mention("<@42> fix it", 42) == "fix it"
    assert strip_bot_mention("<@!42>", 42) == ""
    print("ok intents")


def test_agent_ref() -> None:
    assert is_agent_id("bc-abc-123") and not is_agent_id("abc")
    trailer = ref_trailer("bc-abc-123")
    assert trailer == "-# `bc-abc-123`"
    assert parse_ref(f"Done, PR opened.\n{trailer}") == "bc-abc-123"
    # Older message formats still map threads back to their agent.
    assert parse_ref("||cursor-agent:bc-old-1 · run:r1||") == "bc-old-1"
    assert parse_ref("-# `bc-old-2` · `run-9`") == "bc-old-2"
    assert parse_ref("no id here") is None
    print("ok agent_ref")


def test_reply_chunks() -> None:
    assert split_discord("a" * 4500, 2000) == ["a" * 2000, "a" * 2000, "a" * 500]
    lines = "\n".join(f"line {i}" for i in range(600))
    chunks = split_discord(lines, 2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n".join(chunks) == lines

    done = chat_ui.reply_chunks(TurnResult(agent_id="bc-1", status="finished", text="Fixed it."))
    assert done == ["Fixed it.\n-# `bc-1`"]
    empty = chat_ui.reply_chunks(TurnResult(agent_id="bc-1", status="finished"))
    assert empty[0].startswith("Done.")
    stopped = chat_ui.reply_chunks(TurnResult(agent_id="bc-1", status="cancelled"))
    assert stopped[0].startswith("Stopped.")
    err = chat_ui.reply_chunks(TurnResult(agent_id="bc-1", status="error", error="boom"))
    assert "boom" in err[0]

    long = chat_ui.reply_chunks(TurnResult(agent_id="bc-1", status="finished", text="x " * 6000))
    assert len(long) == chat_ui.MAX_REPLY_MESSAGES
    assert all(len(c) <= 2000 for c in long)
    assert long[-1].endswith("-# `bc-1`") and "cursor.com/agents/bc-1" in long[-1]

    assert chat_ui.pr_button(None) is None
    assert chat_ui.working(A) == "On it, working in `acme/web`…"
    print("ok reply_chunks")


def test_activity_line() -> None:
    tool = SimpleNamespace(type="tool_call", status="running", name="read_file", args={"path": "src/app.py"})
    assert activity_line(tool) == "read file `src/app.py`"
    assert activity_line(SimpleNamespace(type="tool_call", status="completed", name="x", args={})) == ""
    assert activity_line(SimpleNamespace(type="thinking")) == "thinking…"
    assert activity_line(SimpleNamespace(type="status", message="Cloning repo", status="")) == "Cloning repo"
    said = SimpleNamespace(
        type="assistant",
        message=SimpleNamespace(content=[SimpleNamespace(type="text", text="Looking.\nFound the bug.")]),
    )
    assert activity_line(said) == "Found the bug."
    assert is_busy_error(RuntimeError("agent_busy: has an active run"))
    assert not is_busy_error(RuntimeError("repo not found"))
    print("ok activity_line")


def test_cloud_artifacts() -> None:
    art = SdkArtifact(path="artifacts/demo/recording_demo.mp4", size_bytes=100, updated_at="2026-01-01T00:00:00Z")
    assert "artifacts/demo" in artifact_fingerprint(art)
    assert artifact_filename(art.path) == "recording_demo.mp4"
    assert is_image_artifact("artifacts/screenshot.png")
    assert not is_image_artifact("artifacts/log.txt")
    print("ok cloud_artifacts")


def test_multimodal_prompt() -> None:
    plain = AgentPrompt.from_text("fix the button")
    assert plain.sdk_message() == "fix the button"
    img = SDKImage.from_data(b"\x89PNG\r\n", "image/png")
    rich = AgentPrompt(text="look at this", images=(img,))
    assert rich.display.endswith("(+1 image)")
    assert getattr(rich.sdk_message(), "images", None)
    merged = merge_agent_prompts([AgentPrompt.from_text("one"), rich])
    assert merged.text == "one\n\nlook at this" and len(merged.images) == 1
    assert is_image_attachment(SimpleNamespace(content_type="image/png", filename="x.png"))  # type: ignore[arg-type]
    print("ok multimodal_prompt")


class FakeMessage:
    def __init__(self, channel: FakeThread, content: str) -> None:
        self.channel = channel
        self.content = content
        self.view = None

    async def edit(self, *, content: str, view=None) -> None:
        self.content = content
        if view is not None:
            self.view = view


class FakeThread:
    id = 555

    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []

    async def send(self, content: str = "", **kwargs) -> FakeMessage:
        msg = FakeMessage(self, content)
        msg.view = kwargs.get("view")
        self.messages.append(msg)
        return msg


class FakeCursor:
    """Launch blocks until released, so a reply can arrive mid-run."""

    def __init__(self, *, steer_ok: bool) -> None:
        self.steer_ok = steer_ok
        self.release = asyncio.Event()
        self.calls: list[tuple[str, object]] = []

    async def launch(self, prompt, *, repo_url, metadata, on_started, on_activity):
        self.calls.append(("launch", prompt))
        await on_started("bc-test")
        await on_activity("read file `README.md`")
        await self.release.wait()
        return TurnResult(agent_id="bc-test", status="finished", text="Fixed it.", pr_url="https://github.com/acme/web/pull/1")

    async def follow_up(self, agent_id, prompt, *, on_activity):
        self.calls.append(("follow_up", prompt))
        return TurnResult(agent_id=agent_id, status="finished", text="Also done.")

    async def steer(self, agent_id, prompt) -> bool:
        self.calls.append(("steer", prompt))
        return self.steer_ok

    async def list_artifacts(self, agent_id):
        return []


async def _run_conversation(*, steer_ok: bool) -> tuple[FakeCursor, FakeThread]:
    from bot.commands import AgentCog, ThreadState

    cursor = FakeCursor(steer_ok=steer_ok)
    cog = AgentCog(SimpleNamespace(cursor=cursor))  # type: ignore[arg-type]
    thread = FakeThread()
    state = cog.threads[thread.id] = ThreadState()
    status = await thread.send(chat_ui.working(A))
    run = asyncio.create_task(
        cog._converse(thread, state, AgentPrompt.from_text("fix it"), status=status, repo=A)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    assert state.busy and state.agent_id == "bc-test"
    assert "read file" in status.content  # live activity line

    # A reply arrives mid-run: steer, else queue for right after.
    if not (await cog._try_steer("bc-test", AgentPrompt.from_text("also the footer"))):
        state.pending.append(AgentPrompt.from_text("also the footer"))
    cursor.release.set()
    await run
    assert not state.busy and not state.pending
    assert status.content == "Fixed it.\n-# `bc-test`"
    assert status.view is not None  # PR button
    return cursor, thread


def test_conversation_queue() -> None:
    cursor, thread = asyncio.run(_run_conversation(steer_ok=False))
    assert [c[0] for c in cursor.calls] == ["launch", "steer", "follow_up"]
    assert cursor.calls[2][1] == "also the footer"
    assert thread.messages[-1].content == "Also done.\n-# `bc-test`"

    cursor, thread = asyncio.run(_run_conversation(steer_ok=True))
    assert [c[0] for c in cursor.calls] == ["launch", "steer"]
    assert len(thread.messages) == 1
    print("ok conversation_queue")


def main() -> None:
    test_imports()
    test_policy()
    test_repos()
    test_intents()
    test_agent_ref()
    test_reply_chunks()
    test_activity_line()
    test_cloud_artifacts()
    test_multimodal_prompt()
    test_conversation_queue()
    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
