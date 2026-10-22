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
# edit data/.env. Either way, the files the old root-running image left
# behind are handed to that user so SQLite can write them.
#
# This runs as root on a directory the operator controls, so it chowns
# only paths patchbay itself creates, never a tree walk: /data, the env
# file, the database and its SQLite sidecars, and the snapshots directory
# with its direct children. Each must be a regular file or a real
# directory (never a symlink), and a file with another hard link is
# skipped, so a link planted in /data cannot get an unrelated root-owned
# file on the same filesystem chowned. Only root-owned entries are touched.
set -eu

# chown_if_root PATH TYPE: TYPE is f (regular file) or d (directory)
chown_if_root() {
    [ -e "$1" ] && [ ! -L "$1" ] || return 0
    case "$2" in
        f) [ -f "$1" ] && [ "$(stat -c %h "$1")" = 1 ] || return 0 ;;
        d) [ -d "$1" ] || return 0 ;;
    esac
    [ "$(stat -c %u "$1")" = 0 ] || return 0
    chown -h "$uid:$gid" "$1"
}

if [ "$(id -u)" = 0 ]; then
    uid=$(stat -c %u /data)
    gid=$(stat -c %g /data)
    if [ "$uid" = 0 ]; then
        uid=10001
        gid=10001
    fi
    chown_if_root /data d
    for f in /data/.env /data/patchbay.db /data/patchbay.db-wal \
             /data/patchbay.db-shm /data/patchbay.db-journal; do
        chown_if_root "$f" f
    done
    chown_if_root /data/snapshots d
    if [ -d /data/snapshots ] && [ ! -L /data/snapshots ]; then
        for f in /data/snapshots/* /data/snapshots/.[!.]*; do
            chown_if_root "$f" f
        done
    fi
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi
exec "$@"
