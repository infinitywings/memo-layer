#!/bin/sh
# The stack's `memory` service: write the config, create the stack's Core
# project the first time, then gate, export skills, write metrics and sync
# every RKM_JOBS_EVERY seconds. /tmp/ready is the healthcheck: `dsh` starts
# only after this container has written the config.
set -e
python /opt/rka-memory/docker/dsh/stack_config.py
if ! python -c 'import sys; from rka_memory.config import load; sys.exit(0 if load().core_project_id else 1)'; then
  # A new name per ledger: Core refuses a second project with the same name.
  rka-memory init --name "rka-memory stack $(date -u +%Y%m%dT%H%M%SZ)"
fi
touch /tmp/ready
exec rka-memory jobs --every "${RKM_JOBS_EVERY:-300}"
