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
# Cloud Agents API: up to 5 images, 15 MiB each (PNG/JPEG/GIF/WebP).
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES = 5
PROMPT_IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
)

_EMPTY_TEXT_PLACEHOLDER = "(See attached image(s).)"


@dataclass(frozen=True)
class AgentPrompt:
    """Text plus optional images for a cloud agent turn."""

    text: str
    images: tuple[SDKImage, ...] = ()
    actionable: bool = True
    feedback: str = ""

    @classmethod
    def from_text(cls, text: str) -> AgentPrompt:
        stripped = text.strip()
        return cls(text=stripped, actionable=bool(stripped))

    def sdk_message(self) -> str | UserMessage:
        if not self.images:
            return self.text
        return UserMessage(text=self.text, images=self.images)

    @property
    def display(self) -> str:
        """Human-readable prompt for Discord embeds and thread titles."""
        base = self.text or self.feedback
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
        return self.actionable


def merge_agent_prompts(prompts: list[AgentPrompt]) -> AgentPrompt:
    """Merge coalesced Discord messages into one cloud agent turn."""
    if not prompts:
        return AgentPrompt.from_text("")
    if len(prompts) == 1:
        return prompts[0]
    chunks: list[str] = []
    images: list[SDKImage] = []
    feedback_parts: list[str] = []
    for p in prompts:
        if p.text and p.text.strip():
            chunks.append(p.text.strip())
        images.extend(p.images)
        if p.feedback:
            feedback_parts.append(p.feedback)
    images = images[:MAX_IMAGES]
    text = "\n\n".join(chunks)
    if not text and images:
        text = _EMPTY_TEXT_PLACEHOLDER
    return AgentPrompt(
        text=text,
        images=tuple(images),
        actionable=bool(text or images),
        feedback="\n".join(feedback_parts),
    )


def coalesce_ack(*, merged_count: int) -> str:
    """Fallback when steer is unavailable and we must wait for the turn to end."""
    if merged_count <= 1:
        return "Still working — I'll handle this after the current turn."
    return (
        f"Still working — I'll combine your last **{merged_count}** messages "
        "after the current turn."
    )


def _attachment_mime(attachment: discord.Attachment) -> str | None:
    ctype = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    if ctype:
        return ctype
    guessed, _ = mimetypes.guess_type(attachment.filename or "")
    return guessed.lower() if guessed else None


def is_image_attachment(attachment: discord.Attachment) -> bool:
    mime = _attachment_mime(attachment)
    return bool(mime and mime.startswith(IMAGE_MIME_PREFIX))


def is_prompt_image_attachment(attachment: discord.Attachment) -> bool:
    """Raster formats accepted by the Cloud Agents API prompt.images field."""
    mime = _attachment_mime(attachment)
    if not mime or mime == "image/svg+xml":
        return False
    if mime == "image/jpg":
        return True
    return mime in PROMPT_IMAGE_MIMES


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
    file_notes: list[str] = []
    warnings: list[str] = []
    images: list[SDKImage] = []

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        for attachment in message.attachments:
            if is_image_attachment(attachment):
                if len(images) >= MAX_IMAGES:
                    warnings.append(
                        f"- (skipped `{attachment.filename}` — max {MAX_IMAGES} images)"
                    )
                    continue
                if not is_prompt_image_attachment(attachment):
                    warnings.append(
                        f"- `{attachment.filename}` is not a supported prompt image "
                        "(use PNG, JPEG, GIF, or WebP — not SVG)."
                    )
                    continue
                try:
                    images.append(await _download_image(attachment, session))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "failed to load image attachment %s: %s",
                        attachment.filename,
                        exc,
                    )
                    warnings.append(
                        f"- could not load image `{attachment.filename}`: {exc}"
                    )
            else:
                file_notes.append(non_image_attachment_line(attachment))

        actionable = bool(images) or bool(stripped) or bool(file_notes)
        if not actionable:
            return AgentPrompt(
                text="",
                images=tuple(images),
                actionable=False,
                feedback="\n".join(warnings),
            )

        parts: list[str] = []
        if stripped:
            parts.append(stripped)
        elif images:
            parts.append(_EMPTY_TEXT_PLACEHOLDER)
        if file_notes:
            parts.append(
                "**Attachments (not sent as vision):**\n" + "\n".join(file_notes)
            )
        if warnings:
            parts.append("**Attachment notes:**\n" + "\n".join(warnings))
        body = "\n\n".join(parts)

        return AgentPrompt(text=body, images=tuple(images), actionable=True)
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
    if not ctype or ctype not in PROMPT_IMAGE_MIMES:
        ctype = "image/png"
    if ctype == "image/jpg":
        ctype = "image/jpeg"

    async with session.get(attachment.url) as resp:
        resp.raise_for_status()
        data = await resp.read()

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"downloaded image too large ({len(data)} bytes; max {MAX_IMAGE_BYTES})"
        )
    return SDKImage.from_data(data, ctype)
