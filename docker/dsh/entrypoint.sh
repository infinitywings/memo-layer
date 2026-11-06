#!/bin/sh
# First argument is the dsh profile (tui, web, headless); the rest go to it.
set -e
profile="${1:-tui}"
[ $# -gt 0 ] && shift
exec dsh --profile "$profile" --patch /opt/rka-memory/docker/dsh/rka-memory.yml "$@"
