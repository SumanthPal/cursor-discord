# cursor-discord (Cursor)

Discord → [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

**Use:** `@Cursor owner/repo <prompt>` or `/ask` (optional PNG/JPEG/GIF/WebP) · if several repos are allowed, reply with `owner/repo` or a GitHub URL · reply in the thread to steer · Cancel / Open PR on the card · agent demo artifacts post to the thread when a run finishes (`/artifacts` to retry).

## Access

- **Admins** = `SUPERUSER_IDS` (env). They run `/setup repos:owner/a,owner/b` to bind a channel (written to the **channel topic** as `cursor-repo:…`, survives deploys). Admins can also pass `/ask repo:`, or type `@Cursor owner/repo …` in an unbound channel (first use pins it).
- **Members** get access via `/grant @member` (**Cursor** role via `CURSOR_ROLE_NAME`). `/revoke` removes it. Members only work in set-up channels, on that channel's repos: one repo launches directly; several → bot asks, you reply with `owner/repo`.
- Everyone else is denied. If neither `SUPERUSER_IDS` nor `ALLOWED_USER_IDS` is set, everyone is treated as an admin (legacy open mode).

Bot permissions: **Manage Channels** on the work channels (to write topics; the invite bit isn't enough if a channel or category denies it) and **Manage Roles**, with the bot's role above **Cursor**. When it joins a server, the bot posts a setup checklist in the system channel.

When the repo picker appears, use **Search** to filter by owner or repo name (Discord’s dropdown only shows 25 at a time; **Next** pages through the rest).

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
5. Invite the bot with message + slash + Manage Channels + Manage Roles
6. **`SUPERUSER_IDS`** secret (your Discord user ID, comma-separated for several admins)

Optional: `CURSOR_ROLE_NAME`, `ALLOWED_USER_IDS` / `ALLOWED_GUILD_IDS`, YAML nicknames via `PROJECTS_JSON` (see `projects.example.yaml`).
