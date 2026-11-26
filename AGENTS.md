# cursor-discord

Discord bot that launches and drives [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).
Behavior intentionally mirrors Cursor's Slack/Teams integrations; when in doubt about UX,
match what Slack does. User-facing usage and access rules live in `README.md`.

## Setup and checks

Tasks are in the `justfile` (`just` lists them):

```bash
just install   # .venv + pip install -e .
just check     # smoke_check.py + verify_multimodal.py, offline, no Discord/Cursor network
just run       # python -m bot, needs .env (see .env.example)
just up        # docker compose up --build
```

CI (`.github/workflows/ci.yml`) runs exactly the two `just check` scripts on Python 3.12. There is no
pytest suite, linter, or type checker configured. When you change logic that can be tested
offline (parsing, policy, refs, formatting, queueing), add a `test_*` function to
`scripts/smoke_check.py` and call it from `main()` there.

## Layout

All code is in `bot/`. Entry point: `bot/main.py` (`CursorDiscordBot`, plus an aiohttp
`/healthz` server on `$PORT` for Cloud Run).

- `commands.py` — `AgentCog`: every slash command and the `on_message` handler. Large file;
  most features touch it.
- `cursor_parse.py` — parses `@Cursor …` text: `repo=`/`branch=`/`model=`/`autopr=` options and
  subcommands (`agent`, `settings`, `list my agents`, `help`).
- `cursor_agent.py` — `CursorAgentService`, wraps `cursor-sdk` (create, follow-up, stream, cancel).
- `cloud_steer.py` — raw REST call for `delivery: "steer"` (not in the SDK yet).
- `turn_coordinator.py` — per-agent follow-up queue; steers busy runs, debounces bursts.
- `agent_ref.py` — encodes/decodes the agent id stored in thread messages (`cursor-agent:` footer,
  spoiler refs) so a thread can be mapped back to its agent.
- `agent_sessions.py` — in-memory session metadata.
- `channel_bind.py`, `projects.py`, `repo_refs.py` — channel ↔ repo binding and repo matching.
- `policy.py`, `config.py` — env settings, admin/member/guild access checks, rate limiting.
- `multimodal.py`, `thread_context.py` — build agent prompts from Discord messages, images, and
  prior thread history.
- `chat_ui.py`, `agent_cards.py`, `ui.py`, `discord_text.py` — message formatting, status edits,
  views, and Discord length limits.
- `cloud_artifacts.py` — fetch agent artifacts and upload them to Discord.

## Things that are easy to get wrong

- **State is not persisted.** The bot runs on Cloud Run with no database. Durable state lives in
  Discord itself: channel repo bindings in the channel topic (`cursor-repo:…`), agent ids in
  thread messages. Anything in memory is lost on deploy, so new features must recover from
  Discord (or the Cursor API), not from a local cache.
- **Access control.** Admins = `SUPERUSER_IDS`; members need the `CURSOR_ROLE_NAME` role and are
  limited to the channel's bound repos; with no `SUPERUSER_IDS`/`ALLOWED_USER_IDS` everyone is an
  admin. Every new entry point (slash command, message path, button) must go through `Policy`.
  Unauthorized plain messages in agent threads are ignored silently, not answered.
- **Discord limits.** 2000-char messages, 100-char choice values, 25 MB uploads. Use the helpers
  in `discord_text.py` / `repo_refs.py` instead of slicing strings ad hoc.
- **Don't leak ids mid-run.** Agent and run ids are hidden while the status is "working" and shown
  once the run finishes.
- **DMs vs guild threads.** Both paths exist and behave differently (mentions, follow-ups, ref
  lookup); check both when changing message handling.
- Secrets come from env / Secret Manager. Never commit `.env` or put tokens in `projects.yaml`.

## Deploy

Merging to `main` deploys to Cloud Run via `.github/workflows/deploy-cloudrun.yml` (manual path:
`scripts/deploy-cloudrun.sh`). Treat `main` as production: work on a branch and open a PR.
Cursor Bugbot reviews every PR push; address its findings before calling a PR done.

## Conventions

- Python ≥ 3.11, `from __future__ import annotations` at the top of each module, type hints,
  `logging.getLogger(__name__)`.
- Commit messages: short imperative subject describing the behavior change
  (e.g. "Ignore unauthorized plain replies in guild agent threads").
- Update `README.md` and `.env.example` when you add a command, option, or env var.
