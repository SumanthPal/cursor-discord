from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from bot.multimodal import AgentPrompt, coalesce_ack, merge_agent_prompts

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AgentBusyError(Exception):
    """Cloud agent rejected a new turn while a run is still active."""


def is_agent_busy_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "agent_busy" in text or "active run" in text


def chat_queue_ack(*, depth: int) -> str:
    """Back-compat alias — prefer coalesce_ack from multimodal."""
    return coalesce_ack(merged_count=depth)


@dataclass
class FollowUpJob:
    agent_id: str
    agent_prompt: AgentPrompt
    repo: str
    project_alias: str | None
    user_message: object | None = None  # discord.Message
    status_message: object | None = None  # discord.Message — /talk pre-posts status
    coalesce_count: int = 1


@dataclass
class _CoalesceSlot:
    job: FollowUpJob
    prompts: list[AgentPrompt]


class TurnCoordinator:
    """One cloud turn at a time per bc- id; queue Discord follow-ups while busy."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, _CoalesceSlot | None] = {}
        self._last_queue_ack: dict[tuple[str, int], float] = {}
        self._start: Callable[[FollowUpJob], Awaitable[None]] | None = None
        self._ack: Callable[[FollowUpJob, int], Awaitable[None]] | None = None
        self._busy_retries: dict[str, int] = defaultdict(int)

    def set_follow_up_handlers(
        self,
        *,
        start: Callable[[FollowUpJob], Awaitable[None]],
        ack: Callable[[FollowUpJob, int], Awaitable[None]],
    ) -> None:
        self._start = start
        self._ack = ack

    def lock(self, agent_id: str) -> asyncio.Lock:
        return self._locks[agent_id]

    def is_locked(self, agent_id: str) -> bool:
        return self._locks[agent_id].locked()

    async def run_exclusive(
        self, agent_id: str, coro: Callable[[], Awaitable[T]]
    ) -> T:
        async with self._locks[agent_id]:
            result = await coro()
        await self._flush_pending(agent_id)
        return result

    async def _flush_pending(self, agent_id: str) -> None:
        """Start coalesced follow-ups after a create/run releases the turn lock."""
        if self._start is None or self._ack is None:
            return
        slot = self._pending.pop(agent_id, None)
        if slot is None:
            return
        if self._locks[agent_id].locked():
            self._pending[agent_id] = slot
            return
        job = slot.job
        job.agent_prompt = merge_agent_prompts(slot.prompts)
        job.coalesce_count = len(slot.prompts)
        await self._drain(agent_id, job, start=self._start, ack=self._ack)

    def clear_pending(self, agent_id: str) -> None:
        self._pending.pop(agent_id, None)

    async def submit_follow_up(
        self,
        job: FollowUpJob,
        *,
        start: Callable[[FollowUpJob], Awaitable[None]],
        ack: Callable[[FollowUpJob, int], Awaitable[None]],
    ) -> None:
        agent_id = job.agent_id
        lock = self._locks[agent_id]
        if lock.locked():
            slot = self._pending.get(agent_id)
            if slot is None:
                self._pending[agent_id] = _CoalesceSlot(
                    job=job, prompts=[job.agent_prompt]
                )
                await ack(job, 1)
                return
            slot.prompts.append(job.agent_prompt)
            if job.user_message is not None:
                slot.job.user_message = job.user_message
            merged = merge_agent_prompts(slot.prompts)
            slot.job.agent_prompt = merged
            slot.job.coalesce_count = len(slot.prompts)
            await ack(slot.job, slot.job.coalesce_count)
            return
        await self._drain(agent_id, job, start=start, ack=ack)

    async def _drain(
        self,
        agent_id: str,
        first: FollowUpJob,
        *,
        start: Callable[[FollowUpJob], Awaitable[None]],
        ack: Callable[[FollowUpJob, int], Awaitable[None]],
    ) -> None:
        lock = self._locks[agent_id]
        async with lock:
            job: FollowUpJob | None = first
            while job is not None:
                try:
                    await start(job)
                    self._busy_retries[agent_id] = 0
                except AgentBusyError:
                    retries = self._busy_retries[agent_id] + 1
                    self._busy_retries[agent_id] = retries
                    if retries > 30:
                        logger.error(
                            "giving up on follow-up for %s after agent_busy retries",
                            agent_id,
                        )
                        self._busy_retries[agent_id] = 0
                    else:
                        logger.info(
                            "agent busy for %s — retry %s/30",
                            agent_id,
                            retries,
                        )
                        await asyncio.sleep(2.0)
                        continue
                slot = self._pending.pop(agent_id, None)
                if slot is None:
                    job = None
                else:
                    job = slot.job
                    job.agent_prompt = merge_agent_prompts(slot.prompts)
                    job.coalesce_count = len(slot.prompts)

    def should_ack_queue(self, agent_id: str, channel_id: int) -> bool:
        """Debounce queue acks so rapid messages don't spam the thread."""
        key = (agent_id, channel_id)
        now = time.monotonic()
        last = self._last_queue_ack.get(key, 0.0)
        if now - last < 8.0:
            return False
        self._last_queue_ack[key] = now
        return True
