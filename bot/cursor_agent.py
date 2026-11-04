from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cursor_sdk import (
    AsyncClient,
    AsyncRun,
    CloudAgentOptions,
    CloudRepository,
    CursorAgentError,
    RunResult,
    SdkArtifact,
    UserMessage,
)

from bot.turn_coordinator import AgentBusyError, is_agent_busy_error

AgentMessage = str | UserMessage

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class RunOutcome:
    agent_id: str
    run_id: str
    status: str
    result_text: str
    pr_url: str | None = None
    branch: str | None = None
    error_message: str | None = None


class CursorAgentService:
    """Thin async wrapper around cursor-sdk cloud agents."""

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        bridge_workspace: str,
        turns: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.bridge_workspace = bridge_workspace
        self._client: AsyncClient | None = None
        self._active_runs: dict[str, AsyncRun] = {}
        self._turns = turns

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Cursor client not started")
        return self._client

    async def start(self) -> None:
        Path(self.bridge_workspace).mkdir(parents=True, exist_ok=True)
        # Keep a long-lived bridge for the Discord process lifetime.
        self._client = await AsyncClient.launch_bridge(
            workspace=self.bridge_workspace,
            allow_api_key_env_fallback=False,
        )
        logger.info("cursor-sdk AsyncClient bridge ready")

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._active_runs.clear()
        if client is not None:
            await client.aclose()

    async def create_and_run(
        self,
        *,
        prompt: AgentMessage,
        repo_url: str,
        branch: str | None = None,
        model: str | None = None,
        auto_create_pr: bool = True,
        metadata: dict[str, str] | None = None,
        on_status: StatusCallback | None = None,
    ) -> RunOutcome:
        # Omit starting_ref when unset so Cursor resolves the repo default branch
        # (many repos still use master / develop — hardcoding main fails).
        starting = (branch or "").strip() or None
        agent = await self.client.create_agent(
            model=model or self.default_model,
            api_key=self.api_key,
            cloud=CloudAgentOptions(
                repos=[
                    CloudRepository(url=repo_url, starting_ref=starting)
                ],
                auto_create_pr=auto_create_pr,
                skip_reviewer_request=True,
                metadata=metadata or None,
            ),
        )
        agent_id = agent.agent_id
        if on_status:
            await on_status("created", {"agent_id": agent_id})

        async def _send_and_run() -> RunOutcome:
            run = await agent.send(prompt)
            return await self._drive_run(
                agent_id=agent_id, run=run, on_status=on_status
            )

        try:
            if self._turns is not None:
                return await self._turns.run_exclusive(agent_id, _send_and_run)  # type: ignore[union-attr]
            return await _send_and_run()
        finally:
            close = getattr(agent, "aclose", None) or getattr(agent, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result  # type: ignore[misc]

    async def follow_up(
        self,
        *,
        agent_id: str,
        prompt: AgentMessage,
        on_status: StatusCallback | None = None,
    ) -> RunOutcome:
        agent = await self.client.resume_agent(
            agent_id,
            {
                "api_key": self.api_key,
                "model": self.default_model,
            },
        )
        try:
            run = await agent.send(prompt)
            return await self._drive_run(
                agent_id=agent_id, run=run, on_status=on_status
            )
        finally:
            close = getattr(agent, "aclose", None) or getattr(agent, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result  # type: ignore[misc]

    def is_run_active(self, agent_id: str) -> bool:
        return agent_id in self._active_runs

    async def list_repos(self) -> list[str]:
        """URLs Cursor can clone for this API key (GitHub-connected account)."""
        items = await self.client.list_repositories(api_key=self.api_key)
        urls: list[str] = []
        for item in items:
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            if url:
                urls.append(str(url).rstrip("/"))
        # Stable, de-duped
        return sorted(set(urls))

    async def list_artifacts(self, agent_id: str) -> list[SdkArtifact]:
        agent = await self.client.resume_agent(
            agent_id,
            {
                "api_key": self.api_key,
                "model": self.default_model,
            },
        )
        try:
            return await agent.list_artifacts()
        finally:
            await agent.close()

    async def download_artifact(self, agent_id: str, path: str) -> bytes:
        agent = await self.client.resume_agent(
            agent_id,
            {
                "api_key": self.api_key,
                "model": self.default_model,
            },
        )
        try:
            return await agent.download_artifact(path)
        finally:
            await agent.close()

    async def status(self, agent_id: str) -> dict[str, Any]:
        info = await self.client.get_agent(agent_id, api_key=self.api_key)
        active = self._active_runs.get(agent_id)
        return {
            "agent_id": info.agent_id,
            "name": info.name,
            "summary": info.summary,
            "status": info.status,
            "repos": list(info.repos or []),
            "archived": info.archived,
            "active_run_id": getattr(active, "id", None) if active else None,
        }

    async def cancel(self, agent_id: str, run_id: str | None = None) -> str:
        active = self._active_runs.get(agent_id)
        if active is not None:
            if active.supports("cancel"):
                await active.cancel()
                return f"Cancelled active run `{getattr(active, 'id', 'unknown')}`."
            return "Active run does not support cancel."

        target_run_id = run_id
        if not target_run_id:
            runs = await self.client.list_runs(
                agent_id, runtime="cloud", limit=5, api_key=self.api_key
            )
            items = getattr(runs, "items", None)
            if items is None and hasattr(runs, "__aiter__"):
                items = [item async for item in runs]  # type: ignore[misc]
            elif items is None and hasattr(runs, "__iter__"):
                items = list(runs)  # type: ignore[arg-type]
            else:
                items = list(items or [])
            if not items:
                return "No runs found to cancel."
            first = items[0]
            target_run_id = getattr(first, "id", None) or getattr(first, "run_id", None)
            # Prefer cancelling via run handle when available
            if hasattr(first, "supports") and first.supports("cancel"):
                await first.cancel()
                return f"Cancelled run `{target_run_id}`."

        if not target_run_id:
            return "Could not resolve a run id to cancel."
        await self.client.cancel_run(target_run_id, agent_id=agent_id)
        return f"Cancel requested for run `{target_run_id}`."

    async def _drive_run(
        self,
        *,
        agent_id: str,
        run: AsyncRun,
        on_status: StatusCallback | None,
    ) -> RunOutcome:
        run_id = getattr(run, "id", None) or "unknown"
        self._active_runs[agent_id] = run
        if on_status:
            await on_status("running", {"agent_id": agent_id, "run_id": run_id})

        last_text = ""
        try:
            async for message in run.stream():
                text = _assistant_text(message)
                if text:
                    last_text = text
                    if on_status:
                        await on_status(
                            "streaming",
                            {
                                "agent_id": agent_id,
                                "run_id": run_id,
                                "snippet": last_text[-1900:],
                            },
                        )
            result: RunResult = await run.wait()
        except CursorAgentError as err:
            logger.exception("cursor run failed to start: %s", err)
            if on_status:
                await on_status(
                    "error",
                    {"agent_id": agent_id, "run_id": run_id, "error": str(err)},
                )
            return RunOutcome(
                agent_id=agent_id,
                run_id=run_id,
                status="error",
                result_text="",
                error_message=str(err),
            )
        finally:
            self._active_runs.pop(agent_id, None)

        pr_url, branch = _git_bits(result)
        status = str(getattr(result.status, "value", result.status))
        text = (result.result or last_text or "").strip()
        if on_status:
            await on_status(
                status,
                {
                    "agent_id": agent_id,
                    "run_id": result.id,
                    "result": text,
                    "pr_url": pr_url,
                    "branch": branch,
                    "error": result.error_message,
                },
            )
        return RunOutcome(
            agent_id=agent_id,
            run_id=result.id,
            status=status,
            result_text=text,
            pr_url=pr_url,
            branch=branch,
            error_message=result.error_message,
        )


def _assistant_text(message: Any) -> str:
    msg_type = getattr(message, "type", None)
    if msg_type != "assistant" and getattr(message, "role", None) != "assistant":
        # SDKMessage variants
        name = type(message).__name__
        if "Assistant" not in name:
            return ""
    payload = getattr(message, "message", message)
    content = getattr(payload, "content", None)
    if content is None and isinstance(payload, str):
        return payload
    chunks: list[str] = []
    if isinstance(content, list):
        for block in content:
            if getattr(block, "type", None) == "text":
                chunks.append(getattr(block, "text", "") or "")
            elif isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
    elif isinstance(content, str):
        chunks.append(content)
    text_attr = getattr(message, "text", None)
    if text_attr and not chunks:
        chunks.append(str(text_attr))
    return "".join(chunks)


def _git_bits(result: RunResult) -> tuple[str | None, str | None]:
    git = result.git
    if not git or not git.branches:
        return None, None
    first = git.branches[0]
    return (first.pr_url or None), (first.branch or None)


def ensure_api_key_env(api_key: str) -> None:
    """Some SDK paths still read CURSOR_API_KEY; keep process env consistent."""
    os.environ["CURSOR_API_KEY"] = api_key
