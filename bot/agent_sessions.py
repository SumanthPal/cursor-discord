"""In-memory agent session metadata for Discord handoffs (single-owner API key)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AgentSession:
    agent_id: str
    discord_user_id: int
    channel_id: int
    repo: str
    prompt_summary: str
    created_at: float = field(default_factory=time.time)
    status: str = "starting"


def repo_preference_channel_id(channel_id: int, *, parent_id: int | None) -> int:
    """Parent text channel id for threads (repo picker memory)."""
    return parent_id if parent_id is not None else channel_id


class SessionRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, AgentSession] = {}
        self._recent_repo: dict[tuple[int, int], str] = {}

    def remember(
        self,
        *,
        agent_id: str,
        discord_user_id: int,
        channel_id: int,
        repo: str,
        prompt_summary: str,
        repo_preference_channel_id: int | None = None,
    ) -> None:
        pref = repo_preference_channel_id if repo_preference_channel_id is not None else channel_id
        self._by_id[agent_id] = AgentSession(
            agent_id=agent_id,
            discord_user_id=discord_user_id,
            channel_id=channel_id,
            repo=repo,
            prompt_summary=prompt_summary,
        )
        self._recent_repo[(pref, discord_user_id)] = repo

    def recent_repo(self, channel_id: int, user_id: int) -> str | None:
        return self._recent_repo.get((channel_id, user_id))

    def get(self, agent_id: str) -> AgentSession | None:
        return self._by_id.get(agent_id)

    def set_status(self, agent_id: str, status: str) -> None:
        session = self._by_id.get(agent_id)
        if session is not None:
            session.status = status

    def list_for_user(self, user_id: int, *, limit: int = 10) -> list[AgentSession]:
        items = [s for s in self._by_id.values() if s.discord_user_id == user_id]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items[:limit]


def agent_dashboard_url(agent_id: str) -> str:
    return f"https://cursor.com/agents/{agent_id}"
