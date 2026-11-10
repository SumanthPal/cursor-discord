from __future__ import annotations

from bot.multimodal import AgentPrompt

PR_WATCH_MARKER = "[cursor-discord:pr-watch]"


def is_pr_watch_prompt(text: str | None) -> bool:
    return PR_WATCH_MARKER in (text or "")


def user_opted_out_of_pr_watch(text: str | None) -> bool:
    """Allow `@Cursor no-watch …` to skip automatic PR subscriptions."""
    lowered = (text or "").lower()
    return "no-watch" in lowered or "no-pr-watch" in lowered


def pr_watch_discord_ack() -> str:
    return "Subscribe to PR review events (pub/sub — cursor-subscriptions)"


def pr_watch_agent_prompt(*, pr_url: str, branch: str | None = None) -> AgentPrompt:
    return AgentPrompt.from_text(pr_watch_followup_text(pr_url=pr_url, branch=branch))


def pr_watch_followup_text(*, pr_url: str, branch: str | None = None) -> str:
    branch_bit = f"\nHead branch: `{branch}`." if branch else ""
    return f"""{PR_WATCH_MARKER}

The pull request is open: {pr_url}
Switch this cloud agent to **pub/sub** for PR review feedback (no polling loops).

Do this now:
1. Read the subscribe skill if you are unsure of the workflow.
2. Call `cursor-subscriptions-list_subscriptions` and reuse an active PR subscription for this URL if one exists.
3. Call `cursor-subscriptions-subscribe_github_pr` with:
   `{{ "options": {{ "scope": "pr", "prUrl": "{pr_url}" }} }}`
4. **End your turn immediately** after subscribing so the agent sleeps until GitHub delivers an event.

On every wake from PR subscription notifications:
- Re-read the PR (comments, reviews, inline review comments — including Bugbot / `cursor[bot]`).
- **Bugbot / review fixes:** address every unresolved inline review comment and review thread you agree with; push to the PR branch; subscribe to head-branch CI if you need the `Cursor Bugbot` check to settle.
- Re-subscribe to this PR (`subscribe_github_pr`) after each push if subscriptions expired, then end the turn again.
- Call `cursor-subscriptions-unsubscribe` when the PR merged/closed or the user asked to stop.

Tell Discord in one short line that you subscribed to PR events, then end the turn.{branch_bit}"""
