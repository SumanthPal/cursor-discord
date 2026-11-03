#!/usr/bin/env bash
# Push local .env app secrets into:
#   1) GCP Secret Manager (for Cloud Run)
#   2) GitHub Actions secrets (for CI deploy)
#
# Does NOT print secret values.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
# Read KEY=VALUE lines; ignore comments/blank
while IFS= read -r line || [[ -n "${line}" ]]; do
  [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]] && continue
  if [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
    key="${BASH_REMATCH[1]}"
    val="${BASH_REMATCH[2]}"
    # Match python-dotenv: strip one layer of matching quotes.
    if [[ "${val}" =~ ^\"(.*)\"$ ]]; then
      val="${BASH_REMATCH[1]}"
    elif [[ "${val}" =~ ^\'(.*)\'$ ]]; then
      val="${BASH_REMATCH[1]}"
    fi
    export "${key}=${val}"
  fi
done < "${ENV_FILE}"
set +a

: "${DISCORD_TOKEN:?DISCORD_TOKEN missing in ${ENV_FILE}}"
: "${DISCORD_CLIENT_ID:?DISCORD_CLIENT_ID missing in ${ENV_FILE}}"
: "${CURSOR_API_KEY:?CURSOR_API_KEY missing in ${ENV_FILE}}"

GCP_PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
: "${GCP_PROJECT:?Set GCP_PROJECT}"
GCP_REGION="${GCP_REGION:-us-central1}"

echo "Syncing to GCP project=${GCP_PROJECT} (Secret Manager)…"
gcloud config set project "${GCP_PROJECT}" >/dev/null

upsert_sm() {
  local name="$1"
  local value="$2"
  if ! gcloud secrets describe "${name}" >/dev/null 2>&1; then
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null
  echo "  ok ${name}"
}

upsert_sm discord-token "${DISCORD_TOKEN}"
upsert_sm discord-client-id "${DISCORD_CLIENT_ID}"
upsert_sm cursor-api-key "${CURSOR_API_KEY}"

PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for s in discord-token discord-client-id cursor-api-key; do
  gcloud secrets add-iam-policy-binding "${s}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null || true
done

if command -v gh >/dev/null; then
  echo "Syncing to GitHub Actions secrets (gh)…"
  gh secret set DISCORD_TOKEN --body "${DISCORD_TOKEN}"
  gh secret set DISCORD_CLIENT_ID --body "${DISCORD_CLIENT_ID}"
  gh secret set CURSOR_API_KEY --body "${CURSOR_API_KEY}"
  gh secret set GCP_PROJECT --body "${GCP_PROJECT}"
  if [[ -n "${PROJECTS_JSON:-}" ]]; then
    gh secret set PROJECTS_JSON --body "${PROJECTS_JSON}"
  fi
  echo "  ok GitHub secrets (app + GCP_PROJECT)"
  echo "Still required once: GCP_SA_KEY (JSON for a deploy service account)."
else
  echo "gh not installed — skipped GitHub secret sync."
fi

echo "Done."
