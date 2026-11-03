# cursor-discord

Discord command plane for [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

## Commands

| Command | Purpose |
|---|---|
| `/agent prompt:…` | Create cloud agent — **pick a Cursor-connected repo** in chat (or `repo:` autocomplete) |
| `/repos` | List repos linked to the Cursor API key |
| Thread reply | Follow-up on that agent |
| `/talk agent_id: prompt:` | Follow-up any `bc-…` agent |
| `/status` `/cancel` | Status / cancel |
| `/project list` `/project bind` | Optional nickname aliases (YAML), not required |

## Local

```bash
cp .env.example .env   # fill DISCORD_* and CURSOR_API_KEY
cp projects.example.yaml projects.yaml
python -m venv .venv && .venv/bin/pip install -e .
python -m bot
```

Or `docker compose up --build` with `.env` + `projects.yaml`.

## Cloud Run

```bash
export GCP_PROJECT=... GCP_REGION=us-central1
printf '%s' "$DISCORD_TOKEN" | gcloud secrets versions add discord-token --data-file=-
# same for discord-client-id, cursor-api-key
./scripts/deploy-cloudrun.sh
```

Uses min instances = 1, max = 1, `--no-cpu-throttling`, and `/healthz` on `$PORT`.
