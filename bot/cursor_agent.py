"""Cursor cloud agents via cursor-sdk: launch, follow up, steer, cancel, artifacts."""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from cursor_sdk import (
    AsyncAgent,
    AsyncClient,
    AsyncRun,
    CloudAgentOptions,
    CloudRepository,
    CursorAgentError,
    SdkArtifact,
    UserMessage,
)

logger = logging.getLogger(__name__)

AgentMessage = str | UserMessage
OnActivity = Callable[[str], Awaitable[None]]
OnStarted = Callable[[str], Awaitable[None]]

# Steering a live run isn't in cursor-sdk yet; it's one Cloud Agents REST call.
CLOUD_API_BASE = "https://api.cursor.com/v1"


@dataclass(frozen=True)
class TurnResult:
    agent_id: str
    status: str  # finished | error | cancelled | expired
    text: str = ""
    pr_url: str | None = None
    error: str | None = None


def is_busy_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "agent_busy" in text or "active run" in text


class CursorAgentService:
    def __init__(self, *, api_key: str, model: str, bridge_workspace: str) -> None:
        self.api_key = api_key
        self.model = model
        self.bridge_workspace = bridge_workspace
        self._client: AsyncClient | None = None
        self._runs: dict[str, AsyncRun] = {}
        self._starting: set[str] = set()  # send() in flight, run not known yet
        self._cancel_on_start: set[str] = set()

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Cursor client not started")
        return self._client

    async def start(self) -> None:
        # Some SDK paths still read the env var; keep it consistent with settings.
        os.environ["CURSOR_API_KEY"] = self.api_key
        Path(self.bridge_workspace).mkdir(parents=True, exist_ok=True)
        self._client = await AsyncClient.launch_bridge(
            workspace=self.bridge_workspace, allow_api_key_env_fallback=False
        )
        logger.info("cursor-sdk bridge ready (model=%s)", self.model)

    async def close(self) -> None:
        client, self._client = self._client, None
        self._runs.clear()
        if client is not None:
            await client.aclose()

    async def launch(
        self,
        prompt: AgentMessage,
        *,
        repo_url: str,
        metadata: dict[str, str],
        on_started: OnStarted,
        on_activity: OnActivity,
    ) -> TurnResult:
        agent = await self.client.create_agent(
            model=self.model,
            api_key=self.api_key,
            cloud=CloudAgentOptions(
                # No starting_ref: Cursor uses the repo's default branch.
                repos=[CloudRepository(url=repo_url)],
                auto_create_pr=True,
                skip_reviewer_request=True,
                metadata=metadata,
            ),
        )
        await on_started(agent.agent_id)
        return await self._send(agent, prompt, on_activity)

    async def follow_up(
        self, agent_id: str, prompt: AgentMessage, *, on_activity: OnActivity
    ) -> TurnResult:
        agent = await self._resume(agent_id)
        return await self._send(agent, prompt, on_activity)

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self._runs

    async def steer(self, agent_id: str, prompt: AgentMessage) -> bool:
        """Redirect the live run. False if the API wouldn't take it (caller should queue)."""
        payload = {
            "prompt": {"text": prompt} if isinstance(prompt, str) else prompt.to_json(),
            "delivery": "steer",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CLOUD_API_BASE}/agents/{agent_id}/runs",
                json=payload,
                auth=aiohttp.BasicAuth(login=self.api_key, password=""),
            ) as resp:
                if resp.status == 202:
                    return True
                logger.warning(
                    "steer agent=%s → %s %s", agent_id, resp.status, (await resp.text())[:300]
                )
                return False

    async def cancel(self, agent_id: str) -> bool:
        """Cancel the agent's running run. False if nothing was running."""
        run = self._runs.get(agent_id)
        if run is not None:
            await run.cancel()
            return True
        if agent_id in self._starting:
            self._cancel_on_start.add(agent_id)  # _send cancels as soon as the run exists
            return True
        # Not started by this process (e.g. after a redeploy): ask the API.
        runs = await self.client.list_runs(
            agent_id, runtime="cloud", limit=1, api_key=self.api_key
        )
        latest = runs.items[0] if runs.items else None
        if latest is None or latest.status in {"finished", "error", "cancelled", "expired"}:
            return False
        await self.client.cancel_run(latest.id, agent_id=agent_id)
        return True

    async def list_repos(self) -> list[str]:
        """Repos this API key's GitHub connection can clone."""
        items = await self.client.list_repositories(api_key=self.api_key)
        return sorted({str(item.url).rstrip("/") for item in items if item.url})

    async def list_artifacts(self, agent_id: str) -> list[SdkArtifact]:
        agent = await self._resume(agent_id)
        try:
            return await agent.list_artifacts()
        finally:
            await agent.close()

    async def download_artifact(self, agent_id: str, path: str) -> bytes:
        agent = await self._resume(agent_id)
        try:
            return await agent.download_artifact(path)
        finally:
            await agent.close()

    async def _resume(self, agent_id: str) -> AsyncAgent:
        return await self.client.resume_agent(
            agent_id, {"api_key": self.api_key, "model": self.model}
        )

    async def _send(
        self, agent: AsyncAgent, prompt: AgentMessage, on_activity: OnActivity
    ) -> TurnResult:
        agent_id = agent.agent_id
        self._starting.add(agent_id)
        try:
            run = await agent.send(prompt)
            self._starting.discard(agent_id)
            self._runs[agent_id] = run
            if agent_id in self._cancel_on_start:
                await run.cancel()
            last_line = ""
            last_text = ""
            async for message in run.stream():
                last_text = assistant_text(message) or last_text
                line = activity_line(message)
                if line and line != last_line:
                    last_line = line
                    await on_activity(line)
            result = await run.wait()
        except CursorAgentError as err:
            if is_busy_error(err):
                raise
            logger.warning("run failed for %s: %s", agent_id, err)
            return TurnResult(agent_id=agent_id, status="error", error=str(err))
        finally:
            self._starting.discard(agent_id)
            self._cancel_on_start.discard(agent_id)
            self._runs.pop(agent_id, None)
            await agent.close()

        branches = result.git.branches if result.git else ()
        return TurnResult(
            agent_id=agent_id,
            status=str(getattr(result.status, "value", result.status)),
            text=(result.result or last_text).strip(),
            pr_url=(branches[0].pr_url or None) if branches else None,
            error=result.error_message,
        )


def activity_line(message: Any) -> str:
    """One short human-readable line for what the agent is doing right now."""
    kind = getattr(message, "type", None)
    if kind == "tool_call":
        if getattr(message, "status", "") == "completed":
            return ""  # completed tools flicker too fast for Discord edits
        name = str(getattr(message, "name", "") or "tool").replace("_", " ")
        return f"{name}{_tool_detail(getattr(message, 'args', None))}"[:200]
    if kind in {"status", "task"}:
        note = str(getattr(message, "message", "") or getattr(message, "text", "") or "")
        return note.strip()[:200]
    if kind == "thinking":
        return "thinking…"
    if kind == "assistant":
        text = assistant_text(message)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1][-200:] if lines else ""
    return ""


def _tool_detail(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "path", "filePath", "file_path", "target_file", "query", "pattern", "url"):
        val = " ".join(str(args.get(key) or "").split())
        if val:
            return f" `{val[:60] + '…' if len(val) > 60 else val}`"
    return ""


def assistant_text(message: Any) -> str:
    content = getattr(getattr(message, "message", None), "content", None) or ()
    return "".join(
        getattr(block, "text", "") or ""
        for block in content
        if getattr(block, "type", None) == "text"
    )
