#!/usr/bin/env bash
# Example client calls. Set ORCHESTRATOR_API_KEY + HOST first.
: "${HOST:=http://localhost:8765}"
: "${ORCHESTRATOR_API_KEY:?set ORCHESTRATOR_API_KEY first}"
AUTH="X-API-Key: $ORCHESTRATOR_API_KEY"

echo "== create session =="
SID=$(curl -s -X POST "$HOST/sessions" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"cwd":"/workspace","permission_mode":"acceptEdits","allowed_tools":["Read","Bash"]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
echo "session: $SID"

echo "== query =="
curl -s -N -X POST "$HOST/sessions/$SID/query" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"prompt":"List files in cwd and count them."}'

echo "== list =="
curl -s "$HOST/sessions" -H "$AUTH"

echo "== stop =="
curl -s -X DELETE "$HOST/sessions/$SID" -H "$AUTH"
