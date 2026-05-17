# Claude Orchestrator

FastAPI + web UI + Telegram wrapper for the Claude Code CLI, deployable to
Kubernetes with per-user isolated workspaces.

## Run Locally

```bash
cd claude-orchestrator
cp .env.example .env
# Edit .env and replace every placeholder secret.
set -a
. ./.env
set +a
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

Open `http://localhost:8765/`, log in.

## First Admin User

On startup, if the web-user database is empty, the server bootstraps the first
admin user from environment variables:

```bash
export WEB_AUTH_USERNAME=admin
export WEB_AUTH_PASSWORD='<at least 12 chars>'
```

Use `WEB_AUTH_PASSWORD_HASH` instead of `WEB_AUTH_PASSWORD` if you want to pass
a precomputed password hash. Set `WEB_AUTH_TOTP_SECRET` to require 2FA for the
bootstrap admin from the first login.

After logging in as the admin user, open **Users** in the web UI to create more
users, reset passwords, enable or reset 2FA, grant admin access, or disable
accounts. Each authenticated username gets its own isolated workspace under
`/opt/data/users/<user>/`.

After login, open **Passkeys** to enroll a passkey for the current user.
For production passkeys behind a real domain, set `WEB_AUTH_ORIGIN` and
`WEB_AUTH_RP_ID` to match the public HTTPS origin and relying-party ID.

## Telegram and Scheduled Jobs

Telegram support is optional. Set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_ID` to enable it. Users can manage schedules with
`/schedule`, `/jobs`, and `/unschedule`, and agent sessions can create schedules
directly through the bundled `orchestrator-jobs` CLI instead of asking the human
to type a command.

Run the tick endpoint once per minute from cron, Kubernetes CronJob, or another
scheduler:

```bash
curl -fsS -X POST -H "X-API-Key: $ORCHESTRATOR_API_KEY" \
  http://localhost:8765/jobs/tick
```

## Claude Requirements

- `claude` (Claude Code CLI) on `PATH`. The Dockerfile installs it via npm.
- Each user gets an isolated `HOME` at `/opt/data/users/<user>/`. Subscription
  OAuth tokens live at `/opt/data/users/<user>/.claude/.credentials.json`.
- Default model: `CLAUDE_DEFAULT_MODEL` env var (empty = CLI default). Override
  per chat with `/model <name>` in Telegram, or per session in the web UI.

## OAuth Token Refresh

Subscription tokens auto-refresh when the CLI runs, but an **idle pod** never
fires the refresh and tokens expire. A background warmer in
`sessions.oauth_warmer_loop` checks each user's `.credentials.json` every 30
minutes and fires a no-op session when `expiresAt` is within 1 hour.

To bootstrap a fresh pod, seed creds from your host:

```bash
kubectl -n claude-orchestrator cp ~/.claude/.credentials.json \
  claude-orchestrator/$(kubectl -n claude-orchestrator get pod \
    -l app.kubernetes.io/name=claude-orchestrator \
    -o jsonpath='{.items[0].metadata.name}'):/opt/data/users/<user>/.claude/.credentials.json
```

## Ollama Cloud Models

Models with the `:cloud` suffix (`glm-5.1:cloud`, `deepseek-v4-flash:cloud`,
etc.) route through an in-pod Ollama daemon sidecar, which proxies to Ollama
Cloud via your signed-in account. The sidecar listens on `127.0.0.1:11434`.

The orchestrator injects the env that `ollama launch claude` would set:
`ANTHROPIC_BASE_URL=http://127.0.0.1:11434`, `ANTHROPIC_AUTH_TOKEN=ollama`,
`ANTHROPIC_DEFAULT_*_MODEL=<model>`, `CLAUDE_CODE_USE_OPENAI=1`.

### Sign in once per pod

The ed25519 signing key at `/root/.ollama/id_ed25519` (PVC subPath
`ollama-state`) survives pod restarts, so this is a one-time step per
orchestrator deployment:

```bash
kubectl -n claude-orchestrator exec -it -c ollama \
  deploy/claude-orchestrator -- ollama signin
```

The command prints a `https://ollama.com/connect?...` URL. Open it in a
browser, approve, and the command unblocks.

### Verify

```bash
kubectl -n claude-orchestrator exec deploy/claude-orchestrator -c orchestrator -- \
  curl -sS -X POST http://127.0.0.1:11434/v1/messages \
  -H 'content-type: application/json' -H 'x-api-key: ollama' \
  -d '{"model":"glm-5.1:cloud","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'
```

A 200 with `"role":"assistant"` content confirms the daemon → Ollama Cloud
path is wired. Then in Telegram:

```
/model glm-5.1:cloud
/new
<prompt>
```

## HTTP Endpoints

All endpoints accept either an authenticated browser session cookie
(`orchestrator_session`, set by `/auth/login`) or an `X-API-Key` header /
`?api_key=` query string. Service-to-service callers using `X-API-Key`
must also send `X-User: <slug>` to scope the request.

### Auth

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET    | `/auth/status` | cookie (optional) | returns `{authenticated, user, is_admin, totp_required}` |
| POST   | `/auth/login` | body `{username, password, totp?}` | sets `orchestrator_session` cookie. **Rate-limited: 5/60s per source IP.** |
| POST   | `/auth/logout` | none | clears the session cookie |
| GET    | `/auth/passkeys` | cookie/api-key | list passkeys for the current user |
| DELETE | `/auth/passkeys/{credential_id}` | cookie/api-key | remove a passkey owned by the current user |
| POST   | `/auth/passkeys/register/options` | cookie | start WebAuthn registration challenge |
| POST   | `/auth/passkeys/register/verify` | cookie | finish WebAuthn registration |
| POST   | `/auth/passkeys/login/options` | body `{username?}` | start WebAuthn login challenge |
| POST   | `/auth/passkeys/login/verify` | challenge | finish WebAuthn login, sets cookie. **Rate-limited: 5/60s per source IP.** |
| GET    | `/auth/users` | admin | list web users |
| POST   | `/auth/users` | admin | create a web user (optional TOTP secret returned once) |
| PATCH  | `/auth/users/{username}` | admin | update password / admin / disabled / reset TOTP |

### Sessions

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST   | `/sessions` | cookie/api-key | body `{cwd?, system_prompt?, permission_mode, allowed_tools[], max_turns?, model?}`. Cwd defaults to `/opt/data/users/<user>/workspace`. |
| GET    | `/sessions` | cookie/api-key | list active sessions owned by the caller |
| DELETE | `/sessions/{sid}` | cookie/api-key | stop and delete persisted metadata |
| POST   | `/sessions/{sid}/query` | cookie/api-key | NDJSON stream of events: `text`, `tool`, `done` |

### Jobs

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST   | `/jobs/tick` | api-key | **Internal-only.** Caller source IP must be in `INTERNAL_CIDRS` (default `10.42.0.0/16,10.43.0.0/16,127.0.0.0/8`). Called by the cluster CronJob. Returns `{fired:[job_id...], checked_at}`. |

### Hardening notes

- Web auth uses an `HttpOnly`, `Secure`, `SameSite=Lax` cookie signed with
  `WEB_SESSION_SECRET` (defaults to `ORCHESTRATOR_API_KEY` if unset). TTL
  controlled by `WEB_SESSION_TTL_SECONDS` (default 12h).
- `LOGIN_RATE_LIMIT` and `LOGIN_RATE_WINDOW` env vars tune the brute-force
  guard. Rate-limit key is the leftmost `X-Forwarded-For` entry, falling
  back to the TCP peer.
- `INTERNAL_CIDRS` (comma-separated CIDRs) overrides the default internal
  allow-list for `/jobs/tick`.
- `X-API-Key` is effectively a root token: any holder can impersonate any
  user via `X-User`. Treat it as a service credential, not a user token.

## Telegram

Single-user trusted channel. Set `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_ID`, and `TELEGRAM_USER` (slug). Permission mode
defaults to `bypassPermissions` for tool auto-approval.

## Deploy (Kubernetes)

HelmRelease lives in
your GitOps repository. After image push:

```bash
flux reconcile source git flux-system
flux reconcile kustomization claude-orchestrator -n flux-system
```
