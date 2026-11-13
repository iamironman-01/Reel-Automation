#!/bin/sh
# Render mounts the persistent disk at /app/data, and the mount can be owned by root.
# Fix ownership once as root, then run the app as the unprivileged "reeldrop" user.
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
fi

# Already unprivileged (e.g. run locally with --user): just start the app.
exec "$@"
