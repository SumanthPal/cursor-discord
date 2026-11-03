"""Build Cursor UserMessage payloads from Discord messages."""
from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass

import aiohttp
import discord
from cursor_sdk import SDKImage, UserMessage

logger = logging.getLogger(__name__)

IMAGE_MIME_PREFIX = "image/"
# Discord attachment limit is often 8–25 MiB; keep a conservative cap for agent payloads.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 10

_EMPTY_TEXT_PLACEHOLDER = "(See attached image(s).)"


@dataclass(frozen=True)
class AgentPrompt:
    """Text plus optional images for a cloud agent turn."""

    text: str
    images: tuple[SDKImage, ...] = ()

    @classmethod
    def from_text(cls, text: str) -> AgentPrompt:
        return cls(text=text.strip())

    def sdk_message(self) -> str | UserMessage:
        if not self.images:
            return self.text
        return UserMessage(text=self.text, images=self.images)

    @property
    def display(self) -> str:
        """Human-readable prompt for Discord embeds and thread titles."""
        base = self.text
        if self.images:
            n = len(self.images)
            suffix = f" (+{n} image{'s' if n != 1 else ''})"
            if len(base) + len(suffix) <= 2000:
                return base + suffix
            room = max(0, 2000 - len(suffix))
            return base[:room].rstrip() + suffix
        return base

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip()) or bool(self.images)


def is_image_attachment(attachment: discord.Attachment) -> bool:
    ctype = (attachment.content_type or "").lower()
    if ctype.startswith(IMAGE_MIME_PREFIX):
        return True
    guessed, _ = mimetypes.guess_type(attachment.filename or "")
    return bool(guessed and guessed.startswith(IMAGE_MIME_PREFIX))


def non_image_attachment_line(attachment: discord.Attachment) -> str:
    name = attachment.filename or "file"
    ctype = attachment.content_type or "unknown type"
    return f"- [{name}]({attachment.url}) ({ctype}, {attachment.size} bytes)"


async def agent_prompt_from_message(
    message: discord.Message,
    *,
    text: str,
    session: aiohttp.ClientSession | None = None,
) -> AgentPrompt:
    """Merge message text with downloadable image attachments."""
    stripped = text.strip()
    notes: list[str] = []
    images: list[SDKImage] = []

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        for attachment in message.attachments:
            if len(images) >= MAX_IMAGES:
                notes.append(
                    f"- (skipped extra attachments after {MAX_IMAGES} images)"
                )
                break
            if is_image_attachment(attachment):
                try:
                    images.append(await _download_image(attachment, session))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "failed to load image attachment %s: %s",
                        attachment.filename,
                        exc,
                    )
                    notes.append(
                        f"- could not load image `{attachment.filename}`: {exc}"
                    )
            else:
                notes.append(non_image_attachment_line(attachment))

        body = stripped
        if notes:
            block = "**Attachments (not sent as vision):**\n" + "\n".join(notes)
            body = f"{body}\n\n{block}".strip() if body else block

        if not body and images:
            body = _EMPTY_TEXT_PLACEHOLDER
        elif not body and not images:
            body = ""

        return AgentPrompt(text=body, images=tuple(images))
    finally:
        if owns_session and session is not None:
            await session.close()


async def _download_image(
    attachment: discord.Attachment, session: aiohttp.ClientSession
) -> SDKImage:
    if attachment.size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image too large ({attachment.size} bytes; max {MAX_IMAGE_BYTES})"
        )
    ctype = attachment.content_type or mimetypes.guess_type(attachment.filename or "")[
        0
    ]
    if not ctype or not ctype.startswith(IMAGE_MIME_PREFIX):
        ctype = "image/png"

    async with session.get(attachment.url) as resp:
        resp.raise_for_status()
        data = await resp.read()

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"downloaded image too large ({len(data)} bytes; max {MAX_IMAGE_BYTES})"
        )
    return SDKImage.from_data(data, ctype)
