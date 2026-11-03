from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CHANNEL_PROJECT_RE = re.compile(
    r"(?:^|\s)cursor-project:([A-Za-z0-9_./-]+)(?:\s|$)", re.IGNORECASE
)
# Direct repo bind — preferred over YAML aliases for channel defaults.
# Accepts owner/repo or a full https://github.com/owner/repo URL (no spaces).
CHANNEL_REPO_RE = re.compile(
    r"(?:^|\s)cursor-repo:(\S+)(?:\s|$)", re.IGNORECASE
)


@dataclass(frozen=True)
class Project:
    alias: str
    repo: str
    # None → omit startingRef; Cursor uses the repo's default branch.
    branch: str | None = None
    model: str | None = None


@dataclass
class ProjectRegistry:
    projects: dict[str, Project]
    default: str | None = None

    def get(self, alias: str) -> Project | None:
        return self.projects.get(alias.lower())

    def aliases(self) -> list[str]:
        return sorted(self.projects)

    def resolve(
        self,
        *,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        channel_topic: str | None = None,
        default_model: str | None = None,
    ) -> Project | None:
        """Resolution order:

        1. Explicit repo (slash / picker)
        2. Explicit project alias (slash / YAML nicknames)
        3. Channel topic `cursor-repo:…`
        4. Channel topic `cursor-project:…` (YAML nickname)
        5. Registry default alias
        """
        branch_s = _normalize_branch(branch)

        if repo and repo.strip():
            return Project(
                alias="_explicit",
                repo=normalize_repo(repo.strip()),
                branch=branch_s,
                model=default_model,
            )

        alias = (project or "").strip().lower() or None
        if alias:
            found = self.get(alias)
            if not found:
                return None
            return Project(
                alias=found.alias,
                repo=found.repo,
                branch=branch_s if branch_s is not None else found.branch,
                model=found.model or default_model,
            )

        if channel_topic:
            channel_repo = parse_channel_repo(channel_topic)
            if channel_repo:
                return Project(
                    alias="_channel",
                    repo=channel_repo,
                    branch=branch_s,
                    model=default_model,
                )
            alias = parse_channel_project(channel_topic)

        if not alias:
            alias = self.default
        if not alias:
            return None

        found = self.get(alias)
        if not found:
            return None

        return Project(
            alias=found.alias,
            repo=found.repo,
            branch=branch_s if branch_s is not None else found.branch,
            model=found.model or default_model,
        )


def normalize_repo(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    if value.count("/") == 1 and " " not in value:
        return f"https://github.com/{value}"
    return value.rstrip("/")


def _normalize_branch(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def parse_channel_project(topic: str | None) -> str | None:
    if not topic:
        return None
    match = CHANNEL_PROJECT_RE.search(topic)
    if not match:
        return None
    return match.group(1).lower()


def parse_channel_repo(topic: str | None) -> str | None:
    if not topic:
        return None
    match = CHANNEL_REPO_RE.search(topic)
    if not match:
        return None
    return normalize_repo(match.group(1))


def set_channel_project_topic(topic: str | None, alias: str) -> str:
    alias = alias.strip().lower()
    marker = f"cursor-project:{alias}"
    base = topic or ""
    if CHANNEL_PROJECT_RE.search(base):
        return CHANNEL_PROJECT_RE.sub(f" cursor-project:{alias} ", base).strip()
    if not base.strip():
        return marker
    return f"{base.rstrip()} {marker}"


def set_channel_repo_topic(topic: str | None, repo: str) -> str:
    """Upsert `cursor-repo:owner/repo` (compact) into a channel topic."""
    url = normalize_repo(repo)
    # Prefer compact owner/repo in the topic for readability.
    compact = url
    if compact.startswith("https://github.com/"):
        compact = compact.removeprefix("https://github.com/")
    elif compact.startswith("http://github.com/"):
        compact = compact.removeprefix("http://github.com/")
    marker = f"cursor-repo:{compact}"
    base = topic or ""
    if CHANNEL_REPO_RE.search(base):
        return CHANNEL_REPO_RE.sub(f" {marker} ", base).strip()
    if not base.strip():
        return marker
    return f"{base.rstrip()} {marker}"


def _parse_registry(data: dict[str, Any]) -> ProjectRegistry:
    raw_projects = data.get("projects") or {}
    projects: dict[str, Project] = {}
    for alias, cfg in raw_projects.items():
        if not isinstance(cfg, dict):
            continue
        repo = cfg.get("repo") or cfg.get("url")
        if not repo:
            continue
        projects[alias.lower()] = Project(
            alias=alias.lower(),
            repo=normalize_repo(str(repo)),
            branch=_normalize_branch(
                str(cfg["branch"]) if cfg.get("branch") is not None else None
            ),
            model=(str(cfg["model"]) if cfg.get("model") else None),
        )
    default = data.get("default")
    default_s = str(default).lower() if default else None
    if default_s and default_s not in projects:
        default_s = None
    return ProjectRegistry(projects=projects, default=default_s)


def load_registry(
    *,
    projects_json: str | None = None,
    projects_path: str | Path | None = None,
) -> ProjectRegistry:
    if projects_json and projects_json.strip():
        data = json.loads(projects_json)
        if not isinstance(data, dict):
            raise ValueError("PROJECTS_JSON must be a JSON object")
        return _parse_registry(data)

    path = Path(projects_path or "/app/projects.yaml")
    if path.is_file():
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return _parse_registry(data)

    return ProjectRegistry(projects={}, default=None)
