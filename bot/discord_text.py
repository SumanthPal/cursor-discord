from __future__ import annotations

# Discord message content hard limit.
DISCORD_CONTENT_MAX = 2000


def clip_discord(text: str, limit: int = DISCORD_CONTENT_MAX) -> str:
    """Keep a single Discord content body under the API limit."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def split_discord(text: str, limit: int = DISCORD_CONTENT_MAX) -> list[str]:
    """Split long text into message-sized pieces, preferring line breaks."""
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
