#!/usr/bin/env bash
# Minimal MCP Streamable HTTP client for local testing of the Instagram MCP server.
#
# Handles the initialize -> session -> request handshake (the part raw curl makes
# painful), so you can list or call tools with a single command.
#
# Usage:
#   ./scripts/mcp_call.sh list                                  # list tools
#   ./scripts/mcp_call.sh call get_profile_info                 # call a tool (no args)
#   ./scripts/mcp_call.sh call get_media_posts '{"limit":3}'    # call a tool with args
#   ./scripts/mcp_call.sh prompts/list                          # any other MCP method
#   ./scripts/mcp_call.sh <method> '<raw-params-json>'          # generic escape hatch
#
# Environment:
#   MCP_URL   MCP endpoint (default: http://127.0.0.1:8000/mcp)
#   TOKEN     Entra ID bearer JWT. Omit when the server runs with AUTH_ENABLED=false.
#   PROTO     MCP protocol version to negotiate (default: 2025-06-18)
#
# Examples:
#   TOKEN=$(az account get-access-token --resource api://<client-id> --query accessToken -o tsv) \
#     ./scripts/mcp_call.sh call get_profile_info
set -eo pipefail

MCP_URL="${MCP_URL:-http://127.0.0.1:8000/mcp}"
PROTO="${PROTO:-2025-06-18}"

cmd="${1:-list}"
case "$cmd" in
  list|tools/list)
    METHOD="tools/list"; PARAMS="{}" ;;
  call|tools/call)
    name="${2:?usage: $0 call <tool_name> [json_args]}"
    args="${3:-{}}"
    METHOD="tools/call"; PARAMS="{\"name\":\"$name\",\"arguments\":$args}" ;;
  *)
    METHOD="$cmd"; PARAMS="${2:-{}}" ;;
esac

# Build curl args (auth header only when TOKEN is set).
COMMON=(-sS -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
if [ -n "${TOKEN:-}" ]; then
  COMMON+=(-H "Authorization: Bearer ${TOKEN}")
fi

hdr="$(mktemp)"
trap 'rm -f "$hdr"' EXIT

# Extract a result from a (possibly SSE-framed) response body and pretty-print it.
emit() {
  local body="$1" data
  data="$(printf '%s\n' "$body" | sed -n 's/^data: //p')"
  [ -z "$data" ] && data="$body"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$data" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$data"
  else
    printf '%s\n' "$data"
  fi
}

# 1) initialize — the session id comes back in the response headers.
init_body="$(curl "${COMMON[@]}" -D "$hdr" -X POST "$MCP_URL" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"$PROTO\",\"capabilities\":{},\"clientInfo\":{\"name\":\"mcp_call.sh\",\"version\":\"1.0\"}}}")"

http_status="$(sed -n 's/^HTTP[^ ]* \([0-9]*\).*/\1/p' "$hdr" | tail -1)"
SID="$(grep -i '^mcp-session-id:' "$hdr" | awk '{print $2}' | tr -d '\r' || true)"

if [ -z "$SID" ]; then
  echo "ERROR: no Mcp-Session-Id returned (HTTP ${http_status:-?})." >&2
  echo "Hint: 401 => bad/missing token; check TOKEN. Response body:" >&2
  emit "$init_body" >&2
  exit 1
fi

SESSION=(-H "Mcp-Session-Id: $SID")

# 2) tell the server initialization is complete.
curl "${COMMON[@]}" "${SESSION[@]}" -X POST "$MCP_URL" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

# 3) the actual request.
resp="$(curl "${COMMON[@]}" "${SESSION[@]}" -X POST "$MCP_URL" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"$METHOD\",\"params\":$PARAMS}")"
emit "$resp"
