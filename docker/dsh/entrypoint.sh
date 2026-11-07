#!/bin/sh
# First argument is the dsh profile (tui, web, headless); the rest go to it.
# RKM_MODEL=mock routes the agent to the stack's mock-llm service; unset, dsh
# keeps its own default provider (DeepSeek, key in DEEPSEEK_API_KEY).
# The agent's tool processes run as the `agent` user (confine.yml), which
# keeps the root-owned ledger unreadable to them; RKM_CONFINE=0 turns that
# off, for a workspace the agent user cannot write.
set -e
profile="${1:-tui}"
[ $# -gt 0 ] && shift
patches="--patch /opt/rka-memory/docker/dsh/rka-memory.yml"
[ "${RKM_CONFINE:-1}" = 0 ] || patches="$patches --patch ${RKM_CONFINE_OVERLAY:-/opt/rka-memory/docker/dsh/confine.yml}"
case "${RKM_MODEL:-}" in
  "" | deepseek) ;;
  mock) patches="$patches --patch /opt/rka-memory/docker/dsh/mock-route.yml" ;;
  *) echo "RKM_MODEL must be mock or deepseek, not '$RKM_MODEL'" >&2; exit 2 ;;
esac
if [ "${RKM_MODEL:-}" = deepseek ] && [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "RKM_MODEL=deepseek needs DEEPSEEK_API_KEY" >&2
  exit 2
fi
[ -n "${DSH_EXTRA_PATCH:-}" ] && patches="$patches --patch $DSH_EXTRA_PATCH"
# shellcheck disable=SC2086
exec dsh --profile "$profile" $patches "$@"
