"""Cloud Agents REST steer (`delivery: "steer"`) — not exposed on cursor-sdk send yet."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from cursor_sdk import UserMessage

if TYPE_CHECKING:
    from cursor_sdk import SDKImage

logger = logging.getLogger(__name__)

CLOUD_API_BASE = "https://api.cursor.com/v1"


@dataclass(frozen=True)
class SteerOutcome:
    delivery: str  # steered | queued | unavailable
    run_id: str | None = None
    http_status: int | None = None
    detail: str | None = None


def prompt_to_steer_payload(message: str | UserMessage) -> dict[str, object]:
    if isinstance(message, str):
        return {"text": message}
    return UserMessage.from_value(message).to_json()


async def post_steer(
    *,
    api_key: str,
    agent_id: str,
    message: str | UserMessage,
    session: aiohttp.ClientSession | None = None,
) -> SteerOutcome:
    url = f"{CLOUD_API_BASE}/agents/{agent_id}/runs"
    payload = {
        "prompt": prompt_to_steer_payload(message),
        "delivery": "steer",
    }
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        async with session.post(
            url,
            json=payload,
            auth=aiohttp.BasicAuth(login=api_key, password=""),
            headers={"Content-Type": "application/json"},
        ) as resp:
            body_text = await resp.text()
            if resp.status == 403:
                return SteerOutcome(
                    delivery="unavailable",
                    http_status=403,
                    detail=body_text[:500] or None,
                )
            if resp.status == 202:
                try:
                    data = json.loads(body_text) if body_text else {}
                except json.JSONDecodeError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                delivery = str(data.get("delivery") or "steered")
                run_id = data.get("runId") or data.get("run_id")
                return SteerOutcome(
                    delivery=delivery,
                    run_id=str(run_id) if run_id else None,
                    http_status=202,
                )
            logger.warning(
                "steer failed agent=%s status=%s body=%s",
                agent_id,
                resp.status,
                body_text[:300],
            )
            return SteerOutcome(
                delivery="error",
                http_status=resp.status,
                detail=body_text[:500] or None,
            )
    finally:
        if owns and session is not None:
            await session.close()


def steer_ack(outcome: SteerOutcome) -> str:
    if outcome.delivery == "steered":
        return "Got it — redirecting the current run at the next step."
    if outcome.delivery == "queued":
        return (
            "Got it — the run just finished, so this will be handled as the "
            "next turn."
        )
    if outcome.delivery == "unavailable":
        return (
            "Steering isn't available on this account — I'll queue this for "
            "after the current turn."
        )
    return "Got it — I'll apply this when the current turn allows."
