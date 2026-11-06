#!/bin/sh
# dsh's web GUI from inside a container. dsh binds one concrete address, never
# a wildcard, so it binds this container's IP; the host reaches it through a
# 127.0.0.1 port mapping, whose authority must be trusted explicitly. Sign in
# by opening the startup URL in the log: it carries a one-time token.
set -e
ip="$(hostname -i | awk '{print $1}')"
port="${DSH_WEB_PORT:-3080}"
host_port="${DSH_HOST_PORT:-3080}"
extra=""
[ -n "${DSH_EXTRA_PATCH:-}" ] && extra="--patch $DSH_EXTRA_PATCH"
# shellcheck disable=SC2086
exec dsh --profile web --patch /opt/rka-memory/docker/dsh/rka-memory.yml $extra \
  --no-open --host "$ip" --port "$port" \
  --public-url "http://127.0.0.1:${host_port}/" \
  --trusted-host "127.0.0.1:${host_port}" --trusted-host "localhost:${host_port}"
