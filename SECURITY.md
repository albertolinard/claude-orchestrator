# Security

Do not commit runtime credentials or user data.

Keep these outside Git:

- `ORCHESTRATOR_API_KEY`
- `WEB_AUTH_PASSWORD` / `WEB_AUTH_PASSWORD_HASH`
- `WEB_SESSION_SECRET`
- `WEB_AUTH_TOTP_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_ID`
- Claude auth data such as `.claude/.credentials.json`
- SSH keys, kubeconfigs, OAuth files, SQLite databases, and PVC contents

For Kubernetes deployments, store secrets in Kubernetes Secrets or another
secret manager. For local development, use environment variables or an ignored
`.env` file.

Before publishing, run a secret scanner such as `gitleaks` or `trufflehog`
against the repository history.

