# rka-memory: tests in the compose stack (docs/DEV_ENV_PLAN.md).
DOCKER := ./scripts/dockerw
COMPOSE := $(DOCKER) compose
RKA_CORE_SRC ?= /Volumes/FuSpace/Projects/rka-projects/rka
export RKA_CORE_SRC

.PHONY: test test-inner build pull scenarios down clean

# T0 + T1 inside containers: unit tests, plugin tests and Core sync in `dev`,
# then the real-dsh plugin check and the agent-forgery check in `dsh-e2e`.
test: build
	$(COMPOSE) --profile test run --rm dev
	$(COMPOSE) --profile test run --rm dsh-e2e
	$(COMPOSE) --profile test down

build: pull
	$(COMPOSE) --profile test build

# Base images are pulled by the daemon first. Inside a sandboxed Claude Code
# session the build client cannot verify registry TLS itself, and BuildKit
# skips the registry when the image is already local.
pull:
	@for f in docker/dev/Dockerfile docker/dsh/Dockerfile $(RKA_CORE_SRC)/Dockerfile; do \
	  grep -hoE '^FROM [^ ]+|--from=[^ ]+' $$f | sed -E 's/^FROM //; s/^--from=//'; \
	done | grep -E '[:@/]' | sort -u | while read -r img; do $(DOCKER) pull -q "$$img"; done
	$(COMPOSE) --profile test pull --ignore-buildable -q

# T2 + T3: the eight scenario streams through real dsh with the scripted
# model. Results go to the `runs` volume under /runs/workload.
scenarios: pull
	$(COMPOSE) --profile workload build workload
	$(COMPOSE) --profile workload run --rm workload

# Runs inside the `dev` container.
test-inner:
	uv sync --frozen
	uv run pytest -q -p no:cacheprovider
	node --test 'plugins/dsh/test/*.test.ts'

down:
	$(COMPOSE) --profile test --profile interactive --profile workload down

clean:
	$(COMPOSE) --profile test --profile interactive --profile workload down -v
