# cursor-discord

Discord command plane for [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

## How to use (Clippy)

| Action | Purpose |
|---|---|
| `@Clippy <prompt>` or `/ask prompt:…` | Start a cloud agent (repo picker if no channel default) |
| **Remember …** button | Pin `cursor-repo:owner/repo` on the channel topic — next asks skip the picker |
| Reply in the agent **thread** | Steer the same cloud agent |
| Status card buttons | Cancel / Status / Open PR |
| `/ask` + `repo:` | One-shot override of the channel default |
| `/repos` | List Cursor-connected GitHub repos |
| `/talk` `/agent` | Advanced / alias — prefer thread reply and `/ask` |

## Local

```bash
cp .env.example .env   # fill DISCORD_* and CURSOR_API_KEY
cp projects.example.yaml projects.yaml
python -m venv .venv && .venv/bin/pip install -e .
python -m bot
```

Or `docker compose up --build` with `.env` + `projects.yaml`.

## Cloud Run (GitHub Actions)

On every push to `main` (and `workflow_dispatch`), [.github/workflows/deploy-cloudrun.yml](.github/workflows/deploy-cloudrun.yml) builds the image, syncs secrets into Secret Manager, and deploys Cloud Run (`min=1`, `max=1`, `--no-cpu-throttling`, `/healthz` on `$PORT`).

### One-time setup

1. Create a GCP deploy service account with roles roughly:
   - `roles/run.admin`
   - `roles/artifactregistry.writer`
   - `roles/cloudbuild.builds.editor`
   - `roles/secretmanager.admin`
   - `roles/iam.serviceAccountUser`
2. Download a JSON key → GitHub secret **`GCP_SA_KEY`**
3. Set **`GCP_PROJECT`** as a repo variable or secret (`inst-510707`)
4. Sync Discord/Cursor secrets from local `.env` (never commit `.env`):

```bash
export GCP_PROJECT=inst-510707
./scripts/sync-secrets-from-env.sh
```

That writes Secret Manager versions **and** GitHub Actions secrets (`DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `CURSOR_API_KEY`, `GCP_PROJECT`).

Optional GitHub secrets: `PROJECTS_JSON`, `ALLOWED_USER_IDS`, `ALLOWED_GUILD_IDS`, `CURSOR_MODEL`, `DISCORD_GUILD_ID`.

### Manual deploy

```bash
export GCP_PROJECT=inst-510707 GCP_REGION=us-central1
./scripts/sync-secrets-from-env.sh   # if secrets changed
./scripts/deploy-cloudrun.sh
```
