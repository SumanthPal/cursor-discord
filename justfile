# Task runner for cursor-discord. Run `just` to list recipes.

set dotenv-load

venv := ".venv"
py := venv / "bin/python"

# List recipes
default:
    @just --list

# Create the venv and install the package in editable mode
install:
    test -d {{venv}} || python3 -m venv {{venv}}
    {{py}} -m pip install -e .

# Run the bot locally (reads .env)
run: _venv
    {{py}} -m bot

# Run the offline checks CI runs
check: _venv
    {{py}} scripts/smoke_check.py
    {{py}} scripts/verify_multimodal.py

# Run only the smoke checks
smoke: _venv
    {{py}} scripts/smoke_check.py

# Build and run in Docker via docker compose
up:
    docker compose up --build

# Stop the docker compose stack
down:
    docker compose down

# Build the Docker image only
docker-build:
    docker build -t cursor-discord:local .

# Hit the local health endpoint
health port="8080":
    curl -fsS http://127.0.0.1:{{port}}/healthz && echo

# Deploy to Cloud Run manually (needs GCP_PROJECT; CI deploys on push to main)
deploy:
    scripts/deploy-cloudrun.sh

# Push .env secrets to GCP Secret Manager and GitHub Actions secrets
sync-secrets:
    scripts/sync-secrets-from-env.sh

[private]
_venv:
    @test -x {{py}} || (echo "No venv; run 'just install' first" >&2; exit 1)
