# cursor-discord (Clippy)

Discord → [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

**Use:** `@Clippy <prompt>` or `/ask` · reply in the thread to steer · Cancel / Open PR on the card.

First repo pick writes `cursor-repo:owner/repo` into the channel topic (needs **Manage Channels**). After that, asks in that channel skip the picker. Override anytime with `/ask` + `repo:`.

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
