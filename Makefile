# rka-memory: tests in the compose stack (docs/DEV_ENV_PLAN.md).
DOCKER := ./scripts/dockerw
# The interactive stack (`make up`) is compose project rka-memory. Tests and
# scenario runs are project rka-memory-test: their own Core, volumes and
# network, and an ephemeral Core port, so they never touch a running stack.
COMPOSE := $(DOCKER) compose -p rka-memory
TEST_COMPOSE := RKA_STACK_CORE_PORT= $(DOCKER) compose -p rka-memory-test
RKA_CORE_SRC ?= /Volumes/FuSpace/Projects/rka-projects/rka
export RKA_CORE_SRC

.PHONY: test test-inner build pull scenarios live up url check-up down clean

# T0 + T1 inside containers: unit tests, plugin tests and Core sync in `dev`,
# then the real-dsh plugin check and the agent-forgery check in `dsh-e2e`.
test: build
	$(TEST_COMPOSE) --profile test run --rm dev
	$(TEST_COMPOSE) --profile test run --rm dsh-e2e
	$(TEST_COMPOSE) --profile test down

build: pull
	$(TEST_COMPOSE) --profile test build

# Base images are pulled by the daemon first. Inside a sandboxed Claude Code
# session the build client cannot verify registry TLS itself, and BuildKit
# skips the registry when the image is already local.
pull:
	@for f in docker/dev/Dockerfile docker/dsh/Dockerfile $(RKA_CORE_SRC)/Dockerfile; do \
	  grep -hoE '^FROM [^ ]+|--from=[^ ]+' $$f | sed -E 's/^FROM //; s/^--from=//'; \
	done | grep -E '[:@/]' | sort -u | while read -r img; do $(DOCKER) pull -q "$$img"; done
	$(TEST_COMPOSE) --profile test pull --ignore-buildable -q

# T2 + T3: the scenario streams through real dsh with the scripted
# model. Results go to the test project's `runs` volume under /runs/workload.
scenarios: pull
	$(TEST_COMPOSE) --profile workload build workload
	$(TEST_COMPOSE) --profile workload run --rm workload

# T4: the live streams in workload/live, with dsh's default DeepSeek model and
# the DeepSeek miner. Needs DEEPSEEK_API_KEY (in the shell or .env). Each
# stream's report.md lands under workload-runs/ on the host. One stream:
#   make live LIVE=workload/live/2-house-rules.yaml
LIVE ?= workload/live
live: pull
	@[ -n "$$DEEPSEEK_API_KEY" ] || grep -q '^DEEPSEEK_API_KEY=.' .env 2>/dev/null || { echo "make live needs DEEPSEEK_API_KEY in the shell or in .env" >&2; exit 2; }
	$(TEST_COMPOSE) --profile workload build workload
	$(TEST_COMPOSE) --profile workload run --rm -v "$(CURDIR)/workload-runs:/out" workload /opt/rka-memory/$(LIVE) --out /runs/live --copy-to /out

# Interactive: dsh's web UI on 127.0.0.1:$${DSH_HOST_PORT:-3080} with the
# memory service and the stack's Core. The model is the mock unless
# RKM_MODEL=deepseek and DEEPSEEK_API_KEY are set (in the shell or in .env).
up: pull
	$(COMPOSE) --profile interactive build dsh
	$(COMPOSE) --profile interactive up -d
	@$(MAKE) --no-print-directory url

# The sign-in URL from dsh's log; it carries a one-time token.
url:
	@for i in $$(seq 60); do \
	  u=$$($(COMPOSE) --profile interactive logs dsh 2>/dev/null | grep -oE 'http://127\.0\.0\.1:[0-9]+/[^[:space:]]*token[^[:space:]]*' | tail -1); \
	  if [ -n "$$u" ]; then echo "dsh web: $$u"; exit 0; fi; sleep 1; \
	done; echo "dsh web printed no sign-in URL; see: $(COMPOSE) --profile interactive logs dsh" >&2; exit 1

# Smoke check of a running interactive stack (after `make up`).
check-up:
	COMPOSE="$(COMPOSE)" python3 scripts/interactive_check.py

# Runs inside the `dev` container.
test-inner:
	uv sync --frozen
	uv run pytest -q -p no:cacheprovider
	node --test 'plugins/dsh/test/*.test.ts'

# Stops the interactive stack and any leftover test containers; keeps volumes.
down:
	$(COMPOSE) --profile interactive down
	$(TEST_COMPOSE) --profile test --profile workload down

# Also deletes the volumes: the interactive ledger, its Core data and workspaces.
clean:
	$(COMPOSE) --profile interactive down -v
	$(TEST_COMPOSE) --profile test --profile workload down -v
