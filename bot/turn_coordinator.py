from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

from bot.multimodal import AgentPrompt, merge_agent_prompts

logger = logging.getLogger(__name__)

T = TypeVar("T")

STEER_DEBOUNCE_SEC = 0.35


class AgentBusyError(Exception):
    """Cloud agent rejected a new turn while a run is still active."""


def is_agent_busy_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "agent_busy" in text or "active run" in text


@dataclass
class FollowUpJob:
    agent_id: str
    agent_prompt: AgentPrompt
    repo: str
    project_alias: str | None
    user_message: object | None = None  # discord.Message
    status_message: object | None = None  # discord.Message — /talk pre-posts status
    coalesce_count: int = 1
    coalesce_anchor: object | None = None  # first discord.Message in a burst


@dataclass
class _CoalesceSlot:
    job: FollowUpJob
    prompts: list[AgentPrompt]
    first_message: object | None = None


@dataclass
class _SteerBatch:
    job: FollowUpJob
    prompts: list[AgentPrompt] = field(default_factory=list)
    first_message: object | None = None


class TurnCoordinator:
    """One cloud turn at a time per bc- id; steer busy runs via Cloud API."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, _CoalesceSlot | None] = {}
        self._steer_batches: dict[str, _SteerBatch] = {}
        self._steer_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_queue_ack: dict[tuple[str, int], float] = {}
        self._start: Callable[[FollowUpJob], Awaitable[None]] | None = None
        self._ack: Callable[[FollowUpJob, int], Awaitable[None]] | None = None
        self._steer: Callable[[FollowUpJob], Awaitable[None]] | None = None
        self._is_run_active: Callable[[str], bool] | None = None
        self._busy_retries: dict[str, int] = defaultdict(int)

    def set_follow_up_handlers(
        self,
        *,
        start: Callable[[FollowUpJob], Awaitable[None]],
        ack: Callable[[FollowUpJob, int], Awaitable[None]],
    ) -> None:
        self._start = start
        self._ack = ack

    def set_steer_handler(
        self,
        *,
        steer: Callable[[FollowUpJob], Awaitable[None]],
        is_run_active: Callable[[str], bool],
    ) -> None:
        self._steer = steer
        self._is_run_active = is_run_active

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
        job.coalesce_anchor = slot.first_message
        await self._drain(agent_id, job, start=self._start, ack=self._ack)

    def clear_pending(self, agent_id: str) -> None:
        self._pending.pop(agent_id, None)
        self._steer_batches.pop(agent_id, None)
        task = self._steer_tasks.pop(agent_id, None)
        if task is not None and not task.done():
            task.cancel()

    def enqueue_pending(self, job: FollowUpJob) -> None:
        """Fallback when steer fails — wait for the current turn to finish."""
        agent_id = job.agent_id
        slot = self._pending.get(agent_id)
        if slot is None:
            self._pending[agent_id] = _CoalesceSlot(
                job=job,
                prompts=[job.agent_prompt],
                first_message=job.user_message,
            )
        else:
            slot.prompts.append(job.agent_prompt)
            slot.job.agent_prompt = merge_agent_prompts(slot.prompts)
            slot.job.coalesce_count = len(slot.prompts)
        self._schedule_resume_after_active_run(job.agent_id)

    async def flush_pending_turn(self, agent_id: str) -> None:
        """Run queued follow-ups after a turn finishes (create or follow-up path)."""
        await self._flush_pending(agent_id)

    def _schedule_resume_after_active_run(self, agent_id: str) -> None:
        if self._start is None or self._ack is None:
            return
        asyncio.create_task(self._resume_when_idle(agent_id))

    async def _resume_when_idle(self, agent_id: str) -> None:
        """Wait for the active run/lock, then flush pending follow-ups."""
        for _ in range(600):
            active = self._is_run_active is not None and self._is_run_active(
                agent_id
            )
            if not active and not self._locks[agent_id].locked():
                break
            await asyncio.sleep(0.5)
        await self._flush_pending(agent_id)

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
            active = self._is_run_active is not None and self._is_run_active(
                agent_id
            )
            if active and self._steer is not None:
                await self._schedule_steer(job)
                return
            await self._coalesce_while_busy(job, ack=ack)
            return
        await self._drain(agent_id, job, start=start, ack=ack)

    async def _schedule_steer(self, job: FollowUpJob) -> None:
        agent_id = job.agent_id
        batch = self._steer_batches.get(agent_id)
        if batch is None:
            batch = _SteerBatch(
                job=job,
                prompts=[job.agent_prompt],
                first_message=job.user_message,
            )
            self._steer_batches[agent_id] = batch
        else:
            batch.prompts.append(job.agent_prompt)
            if job.user_message is not None:
                batch.job.user_message = job.user_message
        prior = self._steer_tasks.pop(agent_id, None)
        if prior is not None and not prior.done():
            prior.cancel()

        async def _fire() -> None:
            try:
                await asyncio.sleep(STEER_DEBOUNCE_SEC)
                await self._flush_steer(agent_id)
            except asyncio.CancelledError:
                return

        self._steer_tasks[agent_id] = asyncio.create_task(_fire())

    async def _flush_steer(self, agent_id: str) -> None:
        batch = self._steer_batches.pop(agent_id, None)
        self._steer_tasks.pop(agent_id, None)
        if batch is None or self._steer is None:
            return
        merged = merge_agent_prompts(batch.prompts)
        out = batch.job
        out.agent_prompt = merged
        out.coalesce_count = len(batch.prompts)
        out.coalesce_anchor = batch.first_message
        try:
            await self._steer(out)
        except Exception:  # noqa: BLE001
            logger.exception("steer handler failed for %s", agent_id)
            self.enqueue_pending(out)

    async def _coalesce_while_busy(
        self,
        job: FollowUpJob,
        *,
        ack: Callable[[FollowUpJob, int], Awaitable[None]],
    ) -> None:
        agent_id = job.agent_id
        slot = self._pending.get(agent_id)
        if slot is None:
            self._pending[agent_id] = _CoalesceSlot(
                job=job,
                prompts=[job.agent_prompt],
                first_message=job.user_message,
            )
            await ack(job, 1)
            return
        slot.prompts.append(job.agent_prompt)
        if job.user_message is not None:
            slot.job.user_message = job.user_message
        slot.job.agent_prompt = merge_agent_prompts(slot.prompts)
        slot.job.coalesce_count = len(slot.prompts)
        await ack(slot.job, slot.job.coalesce_count)

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
                    job.coalesce_anchor = slot.first_message
        await self._flush_pending(agent_id)

    def should_ack_queue(self, agent_id: str, channel_id: int) -> bool:
        """Debounce acks so rapid messages don't spam the thread."""
        key = (agent_id, channel_id)
        now = time.monotonic()
        last = self._last_queue_ack.get(key, 0.0)
        if now - last < 8.0:
            return False
        self._last_queue_ack[key] = now
        return True
