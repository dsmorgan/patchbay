#!/bin/sh
# Preflight, then run the command. The image runs as uid 10001 and never
# changes ownership or privileges; this script only turns the opaque
# failure of a data directory the container's uid cannot use (a new
# `mkdir data`, root-owned data from an earlier release, or a host ACL:
# "unable to open database file", a silent empty config) into one
# actionable message.
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

# an owner that already matches yet can't write is usually an ACL (Synology
# shares show a "+" after the mode), which chown alone doesn't clear
acl=""
if [ "$(stat -c %u "$data_dir" 2>/dev/null)" = "$(id -u)" ] && [ ! -w "$data_dir" ]; then
    acl="
$data_dir is already owned by uid $(id -u), so its mode or an ACL is
blocking it. An ACL shows as a \"+\" after the mode in \`ls -ld\` on the
host; Synology shares have one. Remove it with the stack stopped, then
re-run the commands below:

    sudo setfacl -bR DATA                              # most Linux hosts
    sudo find DATA -exec synoacltool -del {} \\;    # Synology DSM
"
fi

if [ -n "$bad" ]; then
    cat >&2 <<MSG
patchbay: running as uid $(id -u), which cannot use:$bad
$acl
The image runs as uid 10001, not root, so the host folder mounted at /data
(DATA below: ./data in the quickstart) must be writable by the uid it runs
as. Fix it with the stack stopped, either by giving the data to the image's
user:

    sudo chown -R 10001:10001 DATA
    sudo chmod 600 DATA/.env

or by running the patchbay services as your own user: re-own the data, then
set user: on both patchbay services in compose:

    sudo chown -R "\$(id -u):\$(id -g)" DATA
    chmod 600 DATA/.env
    user: "UID:GID"          # the output of: id -u; id -g

See "Container user and upgrading" in docs/deployment.md.
MSG
    exit 1
fi
exec "$@"
