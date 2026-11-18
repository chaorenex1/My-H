#!/usr/bin/env bash
# mcp-allowlist.sh — manual/CI parity for the harness hook's MCP gate
#
# The omp hook implements this check in TypeScript and does NOT invoke this
# script. Run it yourself for CI/manual verification.
#
# Denies MCP tool invocations whose server is not listed in
# _config.yml.allowed_mcp_servers.
#
# Requires jq when run manually.
# Two tool-name shapes are accepted:
#   - omp:        mcp__<sanitized_server>_<sanitized_tool>
#   - Claude-era: mcp__<server>__<tool>
# omp strips a redundant `<server>_` prefix from the tool name, so a server
# can only be matched by prefix. That is ambiguous when one allow-listed name
# is a prefix of a different server's sanitized name; see scripts.md.
#
# Input: one JSON object on stdin, shaped like
#   {"tool_name":"mcp__filesystem_delete","tool_input":{...}}

set -euo pipefail
# Disable glob expansion: a literal "*" in allowed_mcp_servers would
# otherwise be expanded to filesystem entries by `for a in $allowed`.
set -f

CONFIG_FILE=".harness/_config.yml"

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // ""')"

# Not an MCP call → allow (matcher should have filtered, but be defensive).
case "$tool_name" in
  mcp__*) : ;;
  *) printf '{}\n'; exit 0 ;;
esac

# Extract the name after mcp__. Claude-era names carry <server>__<tool>;
# omp names are <server>_<tool> with no reliable separator, so server
# matching below is prefix-based.
rest="${tool_name#mcp__}"
claude_server=""
case "$rest" in
  *__*) claude_server="${rest%%__*}" ;;
esac

# No config → deny (fail-closed in strict mode is safer than failing open).
if [ ! -f "$CONFIG_FILE" ]; then
  jq -n --arg t "$tool_name" \
    '{decision:"deny", reason:("mcp-allowlist: no _config.yml; denying " + $t)}'
  exit 0
fi

# Parse allowed_mcp_servers from YAML (supports inline "[a, b, c]" form or
# block "- a" form). Fall back to empty list on parse failure.
allowed="$(
  awk '
    /^allowed_mcp_servers:/ {
      rest = $0; sub(/^allowed_mcp_servers:[[:space:]]*/, "", rest)
      if (rest ~ /^\[/) {
        gsub(/[][\047"]/, "", rest); gsub(/,/, " ", rest)
        print rest; exit
      }
      inblock = 1; next
    }
    inblock {
      if ($0 ~ /^[^[:space:]-]/) { inblock = 0; next }
      if ($0 ~ /^[[:space:]]*-[[:space:]]*/) {
        item = $0; sub(/^[[:space:]]*-[[:space:]]*/, "", item)
        gsub(/["\047]/, "", item); printf "%s ", item
      }
    }
  ' "$CONFIG_FILE"
)"

# Sentinel "*" in the allow-list = allow every MCP server. This is the
# "registered MCP servers: all" choice from hearing Round 6. Risky, but
# useful for trusted single-developer projects. We log a one-time risk
# note to progress.md (gated by .harness/.mcp-wildcard-warned) so the
# user has a durable reminder.
for a in $allowed; do
  if [ "$a" = "*" ]; then
    marker=".harness/.mcp-wildcard-warned"
    if [ ! -f "$marker" ]; then
      ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      mkdir -p .harness 2>/dev/null || true
      printf '%s\n' "- ${ts} | WARN mcp-allowlist: wildcard (*) in allow-list — every MCP server is accepted; tighten once the project is hardened" \
        >> .harness/progress.md 2>/dev/null || true
      touch "$marker" 2>/dev/null || true
    fi
    printf '{}\n'
    exit 0
  fi
done

# Allow-list check: exact Claude-era server, or an omp prefix match.
for a in $allowed; do
  if [ -n "$claude_server" ] && [ "$a" = "$claude_server" ]; then
    printf '{}\n'
    exit 0
  fi
  case "$rest" in
    "$a"|"$a"_*)
      printf '{}\n'
      exit 0
      ;;
  esac
done

jq -n --arg s "$rest" --arg t "$tool_name" --arg a "$allowed" \
  '{decision:"deny", reason:("mcp-allowlist: server \"" + $s + "\" not in allow-list [" + $a + "] for tool " + $t)}'
exit 0
