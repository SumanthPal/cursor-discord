# cursor-discord (Cursor)

Discord → [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

**Use:** `@Cursor <prompt>` or `/ask` (optional PNG/JPEG/GIF/WebP on the same message) · reply in the thread to steer · Cancel / Open PR on the card · agent demo artifacts (screenshots/videos) post to the thread when a run finishes · `/artifacts` to fetch any that were missed.

First repo pick writes `cursor-repo:owner/repo` into the channel topic (needs **Manage Channels**). After that, asks in that channel skip the picker. Override anytime with `/ask` + `repo:`.

## Local

```bash
cp .env.example .env   # DISCORD_* + CURSOR_API_KEY
python -m venv .venv && .venv/bin/pip install -e .
python -m bot
```

Or `docker compose up --build`.

### Checks

```bash
pip install -e ".[dev]"
ruff check bot scripts
python scripts/smoke_check.py
python scripts/verify_multimodal.py
```

(Same smoke steps run in [.github/workflows/ci.yml](.github/workflows/ci.yml). Deploy to Cloud Run runs only after CI succeeds on `main`.)

## Cloud Run

Push to `main` (or `workflow_dispatch`) runs [.github/workflows/deploy-cloudrun.yml](.github/workflows/deploy-cloudrun.yml): build → Secret Manager → Cloud Run (`min=1`, no CPU throttling).

### One-time setup

1. GCP deploy SA roughly: `run.admin`, `artifactregistry.writer`, `cloudbuild.builds.editor`, `secretmanager.admin`, `iam.serviceAccountUser`
2. JSON key → GitHub secret **`GCP_SA_KEY`**
3. **`GCP_PROJECT`** repo variable/secret
4. App secrets: **`DISCORD_TOKEN`**, **`DISCORD_CLIENT_ID`**, **`CURSOR_API_KEY`**
5. Invite the bot with message + slash + Manage Channels (for topic pin)

Optional: `ALLOWED_USER_IDS` / `ALLOWED_GUILD_IDS`, YAML nicknames via `PROJECTS_JSON` (see `projects.example.yaml`).
