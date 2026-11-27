# cursor-discord (Cursor)

Talk to [Cursor cloud agents](https://cursor.com/docs/cloud-agent/api/endpoints) in Discord, like you'd talk to a teammate. There are no slash commands to learn.

## Using it

- **Ask for something:** `@Cursor fix the flaky login test`. Attach screenshots if they help. I open a thread on your message, show what I'm doing in a single line, then reply with the result and a **View PR** button. Screenshots and videos the agent makes are posted to the thread.
- **Keep going:** reply in the thread, no `@` needed. If I'm still working, your message redirects the current run (👀 means I saw it). Say *stop* to cancel.
- **Pick a repo:** if the channel has one repo, I use it. If it has several, name one (`@Cursor in acme/web, add dark mode`) or I'll ask which one you mean, and you just answer.
- **Not sure?** `@Cursor` on its own, or `@Cursor what can you do?`

## Access

- **Admins** are the users in `SUPERUSER_IDS`. They set things up by talking to the bot:
  - `@Cursor use owner/repo here` adds a repo to the channel. Name several to add them all. `@Cursor stop using owner/repo here` removes one. The list is saved in the channel **topic** as `cursor-repo:…`, so it survives deploys. An admin can also just ask for a task naming `owner/repo` in a channel with no repos yet, and that repo becomes the channel's default.
  - `@Cursor give @alice access` / `@Cursor remove @alice's access`.
- **Members** with access can use the bot in channels that have repos, on those repos only. "Access" is just the **Cursor Access** role (`CURSOR_ROLE_NAME`), so admins can also hand it out from Discord's own role menu.
- Everyone else is ignored in threads and told they need access when they `@Cursor`. If neither `SUPERUSER_IDS` nor `ALLOWED_USER_IDS` is set, everyone counts as an admin (open mode).

Bot permissions: **Manage Channels** on work channels (to write the topic), **Create Public Threads**, and **Manage Roles** with the bot's role above **Cursor Access**. When it joins a server, the bot posts a short setup note in the system channel.

The default model is `composer-2.5`. Set `CURSOR_MODEL` to override it (e.g. `grok-4.7`). Model ids for your key come from the Cursor SDK's `list_models`.

## Local

```bash
cp .env.example .env   # DISCORD_* + CURSOR_API_KEY
just install
just run               # or: just up (docker compose)
just check             # offline checks CI runs
```

## Cloud Run

Push to `main` (or `workflow_dispatch`) runs [.github/workflows/deploy-cloudrun.yml](.github/workflows/deploy-cloudrun.yml): build → Secret Manager → Cloud Run (`min=1`, no CPU throttling).

### One-time setup

1. GCP deploy SA roughly: `run.admin`, `artifactregistry.writer`, `cloudbuild.builds.editor`, `secretmanager.admin`, `iam.serviceAccountUser`
2. JSON key → GitHub secret **`GCP_SA_KEY`**
3. **`GCP_PROJECT`** repo variable/secret
4. App secrets: **`DISCORD_TOKEN`**, **`DISCORD_CLIENT_ID`**, **`CURSOR_API_KEY`**
5. Invite the bot with Send Messages, Create Public Threads, Manage Channels, and Manage Roles
6. **`SUPERUSER_IDS`** secret (your Discord user ID, comma-separated for several admins)

Optional: `CURSOR_ROLE_NAME`, `CURSOR_MODEL`, `ALLOWED_USER_IDS` / `ALLOWED_GUILD_IDS`.
