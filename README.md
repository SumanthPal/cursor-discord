# cursor-discord (Cursor)

Discord → [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

**Use:** `@Cursor <prompt>` or `/ask` (optional PNG/JPEG/GIF/WebP on the same message) · a thread branches from your `@Cursor` message (or a `/ask` anchor) — the bot replies in plain text inside it; keep chatting there to steer · `/cancel` to stop · PR links appear in the reply · demo artifacts (screenshots/videos) post when a run finishes · `/artifacts` to retry.

No default repo on the channel? The bot asks which repo — reply with the list number or name (no dropdown). First pick can write `cursor-repo:owner/repo` into the channel topic (needs **Manage Channels**). Override anytime with `/ask` + `repo:`.

## Local

```bash
cp .env.example .env   # DISCORD_* + CURSOR_API_KEY
python -m venv .venv && .venv/bin/pip install -e .
python -m bot
```

Or `docker compose up --build`.

## Cloud Run

Push to `main` (or `workflow_dispatch`) runs [.github/workflows/deploy-cloudrun.yml](.github/workflows/deploy-cloudrun.yml): build → Secret Manager → Cloud Run (`min=1`, no CPU throttling).

### One-time setup

1. GCP deploy SA roughly: `run.admin`, `artifactregistry.writer`, `cloudbuild.builds.editor`, `secretmanager.admin`, `iam.serviceAccountUser`
2. JSON key → GitHub secret **`GCP_SA_KEY`**
3. **`GCP_PROJECT`** repo variable/secret
4. App secrets: **`DISCORD_TOKEN`**, **`DISCORD_CLIENT_ID`**, **`CURSOR_API_KEY`**
5. Invite the bot with message + slash + Manage Channels (for topic pin)

Optional: `ALLOWED_USER_IDS` / `ALLOWED_GUILD_IDS`, YAML nicknames via `PROJECTS_JSON` (see `projects.example.yaml`).
