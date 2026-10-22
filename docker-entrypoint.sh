#!/bin/sh
# Preflight, then run the command. The image runs as uid 10001 and never
# changes ownership or privileges; this script only turns the opaque
# failure of a data directory the container's uid cannot use (a new
# `mkdir data`, or root-owned data from an earlier release: "unable to open
# database file", a silent empty config) into one actionable message.
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

The image runs as uid 10001, not root, so the data directory must be
writable by the uid it runs as. Fix it with the stack stopped, either by
giving the data to the image's user:

    sudo chown -R 10001:10001 ./data

or by running the patchbay services as your own user: re-own the data, then
set user: on both patchbay services in compose:

    sudo chown -R "\$(id -u):\$(id -g)" ./data
    user: "UID:GID"          # the output of: id -u; id -g

See "Container user and upgrading" in docs/deployment.md.
MSG
    exit 1
fi
exec "$@"
