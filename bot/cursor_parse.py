"""Parse @Cursor mention text (Slack-style options and subcommands)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from bot.projects import normalize_repo, split_repo_prefix

# repo=owner/repo, branch=feat, model=composer-2.5, autopr=false
_OPTION_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_/-])
    (?P<key>repo|branch|model|autopr)
    \s*=\s*
    (?:
        "(?P<qval>[^"]*)"
        |'(?P<sqval>[^']*)'
        |(?P<val>[^\s]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class MentionKind(str, Enum):
    PROMPT = "prompt"
    HELP = "help"
    SETTINGS = "settings"
    LIST_AGENTS = "list_agents"
    FORCE_AGENT = "force_agent"
    STOP = "stop"


@dataclass(frozen=True)
class LaunchOptions:
    repo: str | None = None
    branch: str | None = None
    model: str | None = None
    auto_create_pr: bool | None = None  # None → platform default (True)


@dataclass(frozen=True)
class ParsedMention:
    kind: MentionKind
    prompt: str = ""
    options: LaunchOptions = LaunchOptions()
    settings_repos: tuple[str, ...] = ()


def _parse_autopr(raw: str) -> bool | None:
    val = raw.strip().lower()
    if val in {"false", "0", "no", "off"}:
        return False
    if val in {"true", "1", "yes", "on"}:
        return True
    return None


def strip_inline_options(text: str) -> tuple[LaunchOptions, str]:
    """Remove key=value options and return structured options + remaining prompt."""
    repo: str | None = None
    branch: str | None = None
    model: str | None = None
    auto_pr: bool | None = None

    def repl(match: re.Match[str]) -> str:
        nonlocal repo, branch, model, auto_pr
        key = match.group("key").lower()
        val = match.group("qval") or match.group("sqval") or match.group("val") or ""
        val = val.strip()
        if key == "repo" and val:
            repo = normalize_repo(val)
        elif key == "branch" and val:
            branch = val
        elif key == "model" and val:
            model = val
        elif key == "autopr":
            auto_pr = _parse_autopr(val)
        return " "

    rest = _OPTION_RE.sub(repl, text or "")
    rest = " ".join(rest.split())
    opts = LaunchOptions(
        repo=repo, branch=branch, model=model, auto_create_pr=auto_pr
    )
    return opts, rest


def parse_settings_repos(text: str) -> tuple[str, ...]:
    """`settings repos: a/b,c/d` or `settings owner/repo`."""
    raw = (text or "").strip()
    lower = raw.lower()
    if lower.startswith("repos:"):
        body = raw.split(":", 1)[1]
    elif lower.startswith("repo:"):
        body = raw.split(":", 1)[1]
    else:
        body = raw
    urls: list[str] = []
    for part in body.replace(";", ",").split(","):
        part = part.strip()
        if part:
            urls.append(normalize_repo(part))
    return tuple(urls)


def parse_mention(raw: str) -> ParsedMention:
    """Classify mention body after the @Cursor ping."""
    opts, body = strip_inline_options(raw or "")
    lowered = body.strip().lower()

    if not lowered:
        return ParsedMention(kind=MentionKind.HELP)

    if lowered in {"help", "commands", "?"}:
        return ParsedMention(kind=MentionKind.HELP, options=opts)

    if lowered.startswith("settings"):
        rest = body[len("settings") :].strip()
        repos = parse_settings_repos(rest) if rest else ()
        return ParsedMention(kind=MentionKind.SETTINGS, settings_repos=repos, options=opts)

    if lowered in {"list my agents", "list agents", "my agents"}:
        return ParsedMention(kind=MentionKind.LIST_AGENTS, options=opts)

    if lowered in {"stop", "cancel", "abort"}:
        return ParsedMention(kind=MentionKind.STOP, options=opts)

    if lowered.startswith("agent "):
        prompt = body[len("agent ") :].strip()
        return ParsedMention(kind=MentionKind.FORCE_AGENT, prompt=prompt, options=opts)

    # Natural repo prefix: owner/repo at start (after options stripped).
    inline_repo, prompt = split_repo_prefix(body)
    if inline_repo and not opts.repo:
        opts = LaunchOptions(
            repo=inline_repo,
            branch=opts.branch,
            model=opts.model,
            auto_create_pr=opts.auto_create_pr,
        )
        body = prompt

    return ParsedMention(kind=MentionKind.PROMPT, prompt=body.strip(), options=opts)


def is_stop_message(content: str, *, bot_user_id: int) -> bool:
    """Plain `stop` in a thread (Slack-style), with or without @Cursor."""
    text = content or ""
    for form in (f"<@{bot_user_id}>", f"<@!{bot_user_id}>"):
        text = text.replace(form, "")
    cleaned = " ".join(text.strip().lower().split())
    return cleaned in {"stop", "cancel", "abort", "@cursor stop", "cursor stop"}


def merge_launch_options(
    parsed: LaunchOptions, project_branch: str | None, project_model: str | None
) -> tuple[str | None, str | None]:
    branch = parsed.branch if parsed.branch is not None else project_branch
    model = parsed.model if parsed.model is not None else project_model
    return branch, model
