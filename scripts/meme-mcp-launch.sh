#!/bin/sh
# meme-mcp-launch.sh
#
# Gate `mcp-remote` on endpoint reachability before launching it.
#
# Why: Claude Desktop spawns MCP servers at app start / after wake-from-sleep.
# If the network path to the cluster isn't ready yet, Node/undici's hard-coded
# 10s connect timeout fires (UND_ERR_CONNECT_TIMEOUT), the proxy process exits,
# and Claude Desktop marks the server "disconnected" with no auto-retry until a
# manual reload. Waiting for /healthz to answer before exec'ing mcp-remote turns
# that fatal race into a brief, silent wait.
set -eu

HEALTH_URL="https://meme.igene.tw/healthz"
MCP_URL="https://meme.igene.tw/mcp/"
MAX_TRIES=60        # ~ up to 2 min of waiting (MAX_TRIES * SLEEP_SECS)
SLEEP_SECS=2

i=0
while [ "$i" -lt "$MAX_TRIES" ]; do
  # -sf: silent, fail on non-2xx. -m 5: cap each probe at 5s. Unauthenticated.
  if curl -sf -m 5 -o /dev/null "$HEALTH_URL"; then
    break
  fi
  i=$((i + 1))
  # stderr shows up in Claude Desktop's MCP log; stdout is the JSON-RPC channel.
  echo "meme-mcp-launch: endpoint not ready (try $i/$MAX_TRIES), retrying..." >&2
  sleep "$SLEEP_SECS"
done

# NOTE: ${AUTH_HEADER} is single-quoted on purpose. The literal string is
# passed through to mcp-remote, which substitutes it from the environment
# itself (matching the original config). The shell never touches the secret.
exec npx -y -p node@24 -p mcp-remote@latest mcp-remote \
  "$MCP_URL" \
  --transport http-only \
  --header 'Authorization:${AUTH_HEADER}'
