# cursor-discord

Discord command plane for [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints).

## Commands

| Command | Purpose |
|---|---|
| `/agent prompt:… [project\|repo]` | Create cloud agent + thread (embed stores `agent_id`) |
| Thread reply | Follow-up on that agent |
| `/talk agent_id: prompt:` | Follow-up any `bc-…` agent |
| `/status` `/cancel` | Status / cancel |
| `/project list` `/project bind` | Aliases + channel topic bind |

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
