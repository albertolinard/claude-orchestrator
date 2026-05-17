#!/usr/bin/env fish
# Launch orchestrator (HTTP API + web UI + optional Telegram bot).
#
# Required:
#   ORCHESTRATOR_API_KEY     (auto-generated if unset)
# Optional (enables Telegram bot):
#   TELEGRAM_BOT_TOKEN        from @BotFather
#   TELEGRAM_ALLOWED_CHAT_ID  your Telegram user/chat ID (0 = open, discovery mode)
# Optional:
#   ORCHESTRATOR_PORT         default 8765

set -e ANTHROPIC_API_KEY  # force subscription auth

if not set -q ORCHESTRATOR_API_KEY
    set -x ORCHESTRATOR_API_KEY (python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
end

if not set -q ORCHESTRATOR_PORT
    set -x ORCHESTRATOR_PORT 8765
end

if not set -q TELEGRAM_ALLOWED_CHAT_ID
    set -x TELEGRAM_ALLOWED_CHAT_ID 0
end

echo "API key:         $ORCHESTRATOR_API_KEY"
echo "Telegram bot:    "(test -n "$TELEGRAM_BOT_TOKEN"; and echo "ENABLED (allowed_chat=$TELEGRAM_ALLOWED_CHAT_ID)"; or echo "disabled (set TELEGRAM_BOT_TOKEN to enable)")
echo "Port:            $ORCHESTRATOR_PORT"
echo "Web UI:          http://localhost:$ORCHESTRATOR_PORT/"

set script_dir (dirname (status --current-filename))
cd $script_dir
python -m uvicorn server:app --host 0.0.0.0 --port $ORCHESTRATOR_PORT
