#!/bin/sh
# dsh sandbox runner for the rka-memory image (sandbox-local `runnerCommand`).
# dsh starts every tool process as
#   confine.sh <bwrap-style profile arguments> -- <command argv>
# This runner drops root to the `agent` user, then applies the same Landlock
# grants dsh's own launcher would: read everywhere, write /dev/null, and in
# workspace-write mode /tmp and the workspace roots. Landlock has no deny
# rules, so the drop is what keeps the ledger out of reach: its directory is
# root-owned and mode 700, and the plugin, which runs inside dsh as root,
# stays the only process that reads or writes it.
set -eu
die() { echo "rka-confine: $*" >&2; exit 125; }

nl='
'
grants="--ro${nl}/${nl}--rw${nl}/dev/null"
while [ $# -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    --tmpfs) grants="$grants${nl}--rw${nl}$2"; shift 2 ;;
    --bind) grants="$grants${nl}--rw${nl}$2"; shift 3 ;;
    --ro-bind) shift 3 ;;
    --dev|--proc) shift 2 ;;
    --unshare-pid|--die-with-parent) shift ;;
    *) die "unknown profile argument: $1" ;;
  esac
done
[ $# -gt 0 ] || die "no command after --"
[ "$(id -u)" = 0 ] || die "must start as root to drop to agent"
id agent >/dev/null 2>&1 || die "no agent user in this image"

IFS="$nl"
# shellcheck disable=SC2086
set -- $grants -- "$@"
unset IFS
exec setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all --no-new-privs \
  -- env HOME=/home/agent /usr/local/bin/landlock-run "$@"
