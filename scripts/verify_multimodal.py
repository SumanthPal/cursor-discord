#!/usr/bin/env python3
"""Offline verification for multimodal input and cloud artifact delivery."""
from __future__ import annotations

import asyncio
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.cloud_artifacts import (  # noqa: E402
    artifact_fingerprint,
    deliver_new_cloud_artifacts,
)
from bot.multimodal import (  # noqa: E402
    AgentPrompt,
    is_prompt_image_attachment,
)
from cursor_sdk import SDKImage, SdkArtifact, UserMessage  # noqa: E402

# Minimal PNG header bytes (not a valid full image; enough for upload tests).
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
    b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class RecordedSend:
    content: str | None
    filenames: list[str] = field(default_factory=list)


class FakeThread:
    """Captures discord.py send() calls without a live bot."""

    def __init__(self) -> None:
        self.sends: list[RecordedSend] = []

    async def send(self, content: str | None = None, files=None, **_kwargs) -> None:
        names: list[str] = []
        if files:
            for f in files:
                names.append(getattr(f, "filename", str(f)))
        self.sends.append(RecordedSend(content=content, filenames=names))


class FakeCursorService:
    def __init__(self) -> None:
        self.artifacts = [
            SdkArtifact(
                path="artifacts/walkthrough/demo.png",
                size_bytes=len(PNG_BYTES),
                updated_at="2026-11-03T09:00:00Z",
            ),
            SdkArtifact(
                path="artifacts/walkthrough/recording_demo.mp4",
                size_bytes=128,
                updated_at="2026-11-03T09:01:00Z",
            ),
        ]

    async def list_artifacts(self, _agent_id: str) -> list[SdkArtifact]:
        return list(self.artifacts)

    async def download_artifact(self, _agent_id: str, path: str) -> bytes:
        if path.endswith(".png"):
            return PNG_BYTES
        if path.endswith(".mp4"):
            return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
        raise FileNotFoundError(path)


class FakeAttachment:
    def __init__(
        self,
        *,
        filename: str,
        content_type: str | None,
        size: int = 100,
        url: str = "https://cdn.discordapp.com/attachments/1/1/x",
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.url = url


def verify_prompt_images() -> None:
    png = FakeAttachment(filename="ui.png", content_type="image/png")
    svg = FakeAttachment(filename="logo.svg", content_type="image/svg+xml")
    assert is_prompt_image_attachment(png)  # type: ignore[arg-type]
    assert not is_prompt_image_attachment(svg)  # type: ignore[arg-type]

    img = SDKImage.from_data(PNG_BYTES, "image/png")
    prompt = AgentPrompt(text="fix this screen", images=(img,))
    msg = prompt.sdk_message()
    assert isinstance(msg, UserMessage)
    assert msg.text == "fix this screen"
    assert msg.images and len(msg.images) == 1
    wire = msg.to_json()
    assert wire["text"] == "fix this screen"
    assert wire["images"]
    print("PASS prompt.images UserMessage wire format")


def verify_strip_mention() -> None:
    from bot.commands import _strip_mention

    class BotUser:
        id = 4242

    assert _strip_mention("<@4242> fix this", BotUser()) == "fix this"  # type: ignore[arg-type]
    assert _strip_mention("<@!4242>", BotUser()) == ""  # type: ignore[arg-type]
    print("PASS mention stripped from thread follow-up text")


def verify_mention_only_with_attachments() -> None:
    from bot.commands import _is_bot_mention_only

    class BotUser:
        id = 999

    class Msg:
        def __init__(self, content: str, attachments: list[object]) -> None:
            self.content = content
            self.attachments = attachments

    bot = BotUser()
    assert _is_bot_mention_only(Msg("<@999>", []), bot)  # type: ignore[arg-type]
    assert not _is_bot_mention_only(
        Msg("<@999>", [object()]), bot
    )  # type: ignore[arg-type]
    assert not _is_bot_mention_only(Msg("", [object()]), bot)  # type: ignore[arg-type]
    print("PASS image-only / mention+image not treated as mention-only")


async def verify_artifact_delivery() -> None:
    thread = FakeThread()
    cursor = FakeCursorService()
    posted: set[str] = set()
    agent_id = "bc-00000000-0000-0000-0000-000000000001"

    result = await deliver_new_cloud_artifacts(
        thread=thread,
        cursor=cursor,  # type: ignore[arg-type]
        agent_id=agent_id,
        posted=posted,
    )
    assert result.delivered == 2, result.delivered
    assert len(thread.sends) >= 1
    all_files = [n for s in thread.sends for n in s.filenames]
    assert "demo.png" in all_files
    assert "recording_demo.mp4" in all_files
    fp_png = artifact_fingerprint(cursor.artifacts[0])
    assert fp_png in posted
    print(
        f"PASS delivered {result.delivered} artifacts to fake thread ({len(all_files)} files)"
    )

    thread2 = FakeThread()
    again = await deliver_new_cloud_artifacts(
        thread=thread2,
        cursor=cursor,  # type: ignore[arg-type]
        agent_id=agent_id,
        posted=posted,
    )
    assert again.delivered == 0
    assert len(thread2.sends) == 0
    print("PASS dedupe: second delivery skipped already-posted artifacts")


async def verify_artifact_retry_after_upload_failure() -> None:
    import discord
    from unittest.mock import MagicMock

    thread = FakeThread()
    cursor = FakeCursorService()
    posted: set[str] = set()
    agent_id = "bc-00000000-0000-0000-0000-000000000002"

    class FailingThread(FakeThread):
        fail_once = True

        async def send(self, content=None, files=None, **_kwargs) -> None:
            if files and self.fail_once:
                self.fail_once = False
                resp = MagicMock()
                resp.status = 500
                resp.reason = "Internal Server Error"
                raise discord.HTTPException(resp, "simulated upload failure")
            await super().send(content=content, files=files)

    failing = FailingThread()
    first = await deliver_new_cloud_artifacts(
        thread=failing,
        cursor=cursor,  # type: ignore[arg-type]
        agent_id=agent_id,
        posted=posted,
    )
    assert first.delivered == 0
    assert not posted
    second = await deliver_new_cloud_artifacts(
        thread=thread,
        cursor=cursor,  # type: ignore[arg-type]
        agent_id=agent_id,
        posted=posted,
    )
    assert second.delivered == 2
    print("PASS artifact retry after failed Discord upload")


def verify_warnings_only() -> None:
    bad = AgentPrompt(text="", actionable=False, feedback="- svg rejected")
    assert not bad.has_content
    assert bad.feedback
    print("PASS warnings-only prompts are not actionable")


async def main() -> None:
    verify_prompt_images()
    verify_strip_mention()
    verify_warnings_only()
    verify_mention_only_with_attachments()
    await verify_artifact_delivery()
    await verify_artifact_retry_after_upload_failure()
    print("\nVERIFICATION OK — multimodal input + cloud artifact delivery")


if __name__ == "__main__":
    asyncio.run(main())
