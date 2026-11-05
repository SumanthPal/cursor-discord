from __future__ import annotations

# Discord message content hard limit (embeds use a higher cap).
DISCORD_CONTENT_MAX = 2000


def clip_discord(text: str, limit: int = DISCORD_CONTENT_MAX) -> str:
    """Keep a single Discord content body under the API limit."""
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(0, limit - len(suffix))] + suffix


def chunk_discord_lines(
    header: str,
    lines: list[str],
    *,
    footer: str = "",
    limit: int = DISCORD_CONTENT_MAX,
) -> list[str]:
    """Pack lines into Discord messages under `limit` chars each.

    When `footer` is set, every page reserves space for it so the last page
    can append the footer without dropping lines or spinning on rsplit.
    """
    footer_room = (1 + len(footer)) if footer else 0
    body_limit = max(1, limit - footer_room)
    chunks: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= body_limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = clip_discord(line, body_limit) if len(line) > body_limit else line
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [header or "_(empty)_"]
    if footer:
        last = chunks[-1]
        joined = f"{last}\n{footer}" if last else footer
        chunks[-1] = clip_discord(joined, limit)
    return [clip_discord(c, limit) for c in chunks]
