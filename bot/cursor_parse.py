"""Work out what someone means when they talk to the bot.

Everything is plain language: a handful of short phrasings are recognized as
stop / admin requests, and anything else is a task for the agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from bot.repos import repo_tokens

_STOP_PHRASES = frozenset(
    {
        "stop", "cancel", "abort", "halt", "never mind", "nevermind", "nvm",
        "stop it", "stop that", "cancel that", "cancel it", "please stop", "stop please",
    }
)
# Words allowed around repo names in "use owner/repo here"-style messages.
_BIND_FILLER = frozenset(
    {
        "use", "using", "work", "working", "in", "on", "with", "here", "this", "channel",
        "for", "repo", "repos", "the", "and", "&", "please", "pls", "pin", "bind", "set",
        "to", "from", "only", "also", "add", "stop", "remove", "forget", "unbind", "drop",
        "don't", "dont", "no", "longer", "anymore",
    }
)
_UNBIND_WORDS = frozenset({"stop", "remove", "forget", "unbind", "drop", "longer", "anymore"})
_REVOKE_WORDS = frozenset({"revoke", "remove", "take", "drop", "kick"})
_GRANT_WORDS = frozenset({"give", "grant", "add", "let", "allow"})
# Every word of a grant/revoke request (besides the mentions) must be one of these,
# so "remove @bob from the reviewers list" stays a task.
_ACCESS_FILLER = _REVOKE_WORDS | _GRANT_WORDS | frozenset(
    {"access", "'s", "’s", "away", "to", "from", "use", "you", "cursor", "the", "bot",
     "me", "please", "pls", "and", "&", "their", "his", "her", "of"}
)
_GREETINGS = frozenset(
    {"", "hi", "hello", "hey", "yo", "help", "?", "what can you do", "what can you do?",
     "how do i use you", "how do i use you?", "how does this work", "how does this work?"}
)
_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>")
MAX_ADMIN_WORDS = 8


class Intent(str, Enum):
    TASK = "task"
    HELLO = "hello"
    STOP = "stop"
    BIND = "bind"
    UNBIND = "unbind"
    GRANT = "grant"
    REVOKE = "revoke"


@dataclass(frozen=True)
class Parsed:
    intent: Intent
    repos: list[str] = field(default_factory=list)


def _words(text: str) -> list[str]:
    cleaned = _MENTION_RE.sub(" ", text or "").lower().replace("’", "'")
    cleaned = re.sub(r"(?<=\s)'s\b|^'s\b", " 's", cleaned)
    cleaned = re.sub(r"[,.!;:]+(?=\s|$)", " ", cleaned)
    cleaned = re.sub(r"(?<!\S)[-–—:=>]+(?!\S)", " ", cleaned)  # "use this - acme/web"
    return cleaned.split()


def is_stop(text: str) -> bool:
    return " ".join(_words(text)) in _STOP_PHRASES


def classify(text: str, *, mentions_people: bool = False, has_files: bool = False) -> Parsed:
    """``text`` is the message with the bot mention removed (other mentions kept)."""
    words = _words(text)
    joined = " ".join(words)

    if not has_files and joined in _GREETINGS:
        return Parsed(Intent.HELLO)
    if is_stop(text):
        return Parsed(Intent.STOP)

    if mentions_people and words and len(words) <= MAX_ADMIN_WORDS and set(words) <= _ACCESS_FILLER:
        about_access = "access" in words or "use" in words
        if _REVOKE_WORDS & set(words) and (about_access or words[0] == "revoke"):
            return Parsed(Intent.REVOKE)
        if _GRANT_WORDS & set(words) and (about_access or words[0] == "grant"):
            return Parsed(Intent.GRANT)

    repos = repo_tokens(text)
    if repos and not has_files:
        leftover = [w for w in _words(_strip_repo_words(text)) if w not in _BIND_FILLER]
        if not leftover:
            intent = Intent.UNBIND if _UNBIND_WORDS & set(words) else Intent.BIND
            return Parsed(intent, repos)

    return Parsed(Intent.TASK)


def _strip_repo_words(text: str) -> str:
    """Drop URL / owner/repo words so only the surrounding phrasing is left."""
    out = []
    for word in (text or "").split():
        if "/" in word:
            continue
        out.append(word)
    return " ".join(out)


def strip_bot_mention(content: str, bot_user_id: int) -> str:
    for form in (f"<@{bot_user_id}>", f"<@!{bot_user_id}>"):
        content = content.replace(form, "")
    return content.strip()
