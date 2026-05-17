# Claude Orchestrator

FastAPI + web UI + Telegram wrapper for the Claude Code CLI, deployable to
Kubernetes with per-user isolated workspaces.

## Run Locally

```bash
cd claude-orchestrator
export ORCHESTRATOR_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export WEB_AUTH_USERNAME=admin
export WEB_AUTH_PASSWORD='<at least 12 chars>'
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

Open `http://localhost:8765/`, log in.

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
