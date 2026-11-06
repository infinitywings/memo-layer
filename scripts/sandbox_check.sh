#!/bin/sh
# Inside the rka-memory-dsh image: can the agent's own tools forge ledger lines?
# The mock model tries a `write` into /rka-memory and a bash append to the
# ledger, then an escalated retry. Prints PASS/FAIL; exits non-zero on FAIL.
#   docker run --rm --entrypoint sh rka-memory-dsh /opt/rka-memory/scripts/sandbox_check.sh
set -e
mkdir -p /rka-memory /tmp/p && cd /tmp/p && git init -q && touch uv.lock
printf 'packet:\n  arms: [gated]\nminer:\n  backend: none\ncore:\n  sync_on_session_end: false\n' > /rka-memory/config.yaml
cat > /tmp/route.yml <<YML
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      mock:
        apiKeyEnv: MOCK_LLM_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:18500/v1
        models:
          - id: mock-model
            contextWindow: 128000
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: mock
    model: mock-model
YML
export MOCK_LLM_KEY=k MOCK_WRITE_PATH=/rka-memory/forged.txt MOCK_BASH_COMMAND='echo FORGED-BY-AGENT >> /rka-memory/ledger.jsonl'
uv run --no-dev python /opt/rka-memory/scripts/mock_openai.py 18500 /tmp/req.jsonl /tmp/p >/dev/null 2>&1 &
sleep 2
fail=0
run() {
  dsh --profile headless --patch /opt/rka-memory/docker/dsh/rka-memory.yml --patch /tmp/route.yml "$@" "try" >/dev/null 2>&1 || true
  if [ "$(grep -cx FORGED-BY-AGENT /rka-memory/ledger.jsonl || true)" = 0 ] && [ ! -e /rka-memory/forged.txt ]; then
    echo "PASS  agent could not write the ledger ($*)"
  else
    echo "FAIL  agent wrote the ledger ($*)"; fail=1
  fi
}
run --approval-policy never
run --approval-policy never --permission-mode read-only
MOCK_BASH_ESCALATE=danger-full-access run --approval-policy never
MOCK_BASH_ESCALATE=danger-full-access run --approval-policy ask
exit $fail
