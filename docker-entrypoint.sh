#!/bin/sh
# Drop root before running patchbay. Started as root (the default), this
# fixes the ownership of /data and then runs the command unprivileged.
# Started with a non-root user (compose `user:`, `docker run --user`), it
# just runs the command; chown is then the operator's job.
#
# Which unprivileged user: whoever owns /data. A root-owned /data (an
# install from before the image dropped root, a fresh named volume, a
# directory Docker created for a missing bind mount) becomes the image's
# patchbay user, 10001. A /data owned by the host user (the usual
# `mkdir data` bind mount) keeps that owner, so the host user can still
# edit data/.env. Either way, files the old root-running image left
# behind (patchbay.db, snapshots) are handed to that user so SQLite can
# write them. Entries owned by anyone else are never touched.
set -eu

if [ "$(id -u)" = 0 ]; then
    uid=$(stat -c %u /data)
    gid=$(stat -c %g /data)
    if [ "$uid" = 0 ]; then
        uid=10001
        gid=10001
        chown "$uid:$gid" /data
    fi
    # -xdev: stay on the volume. -user 0 + -h: only root-owned entries,
    # and never follow a symlink out of /data.
    find /data -xdev -user 0 -exec chown -h "$uid:$gid" {} +
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi
exec "$@"
