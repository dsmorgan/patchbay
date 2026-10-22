#!/bin/sh
# Preflight, then run the command. The image runs as uid 10001 and never
# changes ownership or privileges; this script only turns the opaque
# failure of an old root-owned data directory ("unable to open database
# file", a silent empty config) into one actionable message.
set -eu

env_file=${PATCHBAY_ENV:-/data/.env}
db=${PATCHBAY_DB:-/data/patchbay.db}
data_dir=$(dirname "$db")
snap_dir=${PATCHBAY_SNAPSHOT_DIR:-$data_dir/snapshots}

bad=""
nl="
"
# a path that exists but the current user cannot use; absent paths are fine,
# the app creates them (when the directory itself is writable)
[ -w "$data_dir" ] || bad="$bad$nl  $data_dir (not writable)"
[ ! -e "$env_file" ] || [ -r "$env_file" ] || bad="$bad$nl  $env_file (not readable)"
[ ! -e "$db" ] || { [ -r "$db" ] && [ -w "$db" ]; } || bad="$bad$nl  $db (not writable)"
[ ! -d "$snap_dir" ] || [ -w "$snap_dir" ] || bad="$bad$nl  $snap_dir (not writable)"

if [ -n "$bad" ]; then
    cat >&2 <<MSG
patchbay: running as uid $(id -u), which cannot use:$bad

The image runs as uid 10001, not root. Data written by an earlier release
is root-owned. Fix it once, with the stack stopped:

    sudo chown -R 10001:10001 ./data

or run the services as the data owner instead, in compose:

    user: "UID:GID"          # for example the output of: stat -c %u:%g ./data

See "Container user and upgrading" in docs/deployment.md.
MSG
    exit 1
fi
exec "$@"
