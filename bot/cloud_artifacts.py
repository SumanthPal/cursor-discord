"""List and post Cursor cloud agent workspace artifacts to Discord."""
from __future__ import annotations

import io
import logging
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import discord
from cursor_sdk import SdkArtifact

if TYPE_CHECKING:
    from bot.cursor_agent import CursorAgentService

logger = logging.getLogger(__name__)

# Discord bot upload limit (bytes).
DISCORD_MAX_UPLOAD = 25 * 1024 * 1024
# Cap files pulled per delivery pass (avoid huge threads).
MAX_ARTIFACTS_PER_DELIVERY = 20

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".m4v"})


def artifact_fingerprint(artifact: SdkArtifact) -> str:
    """Stable id for deduping across follow-up runs."""
    return f"{artifact.path}|{artifact.updated_at}|{artifact.size_bytes}"


def artifact_filename(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name
    return name or "artifact"


def is_image_artifact(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _IMAGE_SUFFIXES


def is_video_artifact(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _VIDEO_SUFFIXES


def guess_filename(path: str, data: bytes) -> str:
    name = artifact_filename(path)
    if "." in name:
        return name
    guessed, _ = mimetypes.guess_type(name)
    if guessed == "image/png":
        return f"{name}.png"
    if guessed == "image/jpeg":
        return f"{name}.jpg"
    return name


async def deliver_new_cloud_artifacts(
    *,
    thread: discord.abc.Messageable,
    cursor: CursorAgentService,
    agent_id: str,
    posted: set[str],
) -> int:
    """Download and upload artifacts not yet posted for this agent. Returns count sent."""
    try:
        items = await cursor.list_artifacts(agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_artifacts failed for %s: %s", agent_id, exc)
        return 0

    pending = [
        item
        for item in items
        if artifact_fingerprint(item) not in posted
        and (item.path or "").startswith("artifacts/")
    ]
    pending.sort(key=lambda a: a.path)
    if not pending:
        return 0

    pending = pending[:MAX_ARTIFACTS_PER_DELIVERY]

    delivered = 0
    batch_files: list[discord.File] = []
    batch_labels: list[str] = []
    batch_fps: list[str] = []
    header_sent = False

    def header() -> str:
        return (
            f"**Agent artifacts** ({len(pending)} new)"
            if len(pending) > 1
            else "**Agent artifact**"
        )

    async def flush_batch() -> list[str]:
        """Upload the current batch; return fingerprints to mark posted on success."""
        nonlocal batch_files, batch_labels, batch_fps, header_sent
        if not batch_files:
            return []
        fps = list(batch_fps)
        prefix = header() if not header_sent else ""
        header_sent = True
        content = "\n".join(f"• `{label}`" for label in batch_labels)
        if prefix:
            content = f"{prefix}\n{content}"
        files = batch_files
        batch_files = []
        batch_labels = []
        batch_fps = []
        try:
            await thread.send(content=content, files=files)
            return fps
        except discord.HTTPException:
            logger.warning("artifact batch upload failed", exc_info=True)
            return []

    for artifact in pending:
        fp = artifact_fingerprint(artifact)
        try:
            data = await cursor.download_artifact(agent_id, artifact.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("download_artifact %s failed: %s", artifact.path, exc)
            await thread.send(
                f"Could not download `{artifact.path}`: {exc}",
            )
            continue

        name = guess_filename(artifact.path, data)
        if len(data) > DISCORD_MAX_UPLOAD:
            posted.add(fp)
            await thread.send(
                f"`{artifact.path}` is {len(data)} bytes — over Discord's upload limit. "
                f"Open the run on [cursor.com/agents](https://cursor.com/agents) to view it."
            )
            continue

        if is_image_artifact(artifact.path) or is_video_artifact(artifact.path):
            if len(batch_files) >= 10:
                done = await flush_batch()
                delivered += len(done)
                posted.update(done)
            batch_files.append(discord.File(io.BytesIO(data), filename=name))
            batch_labels.append(name)
            batch_fps.append(fp)
            continue

        try:
            prefix = header() if not header_sent else ""
            header_sent = True
            body = f"{prefix}\n`{name}`" if prefix else f"`{name}`"
            await thread.send(
                content=body,
                file=discord.File(io.BytesIO(data), filename=name),
            )
            posted.add(fp)
            delivered += 1
        except discord.HTTPException:
            logger.warning("artifact upload failed for %s", artifact.path, exc_info=True)

    done = await flush_batch()
    delivered += len(done)
    posted.update(done)

    return delivered
