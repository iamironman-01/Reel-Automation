#!/usr/bin/env bash
# ReelDrop backup: archives data/ (SQLite DB, session.json, logs) and config/
# into a dated .tgz, then deletes old backups.
#
# Usage:
#   scripts/backup.sh [BACKUP_DIR]
#
# Settings (argument beats environment variable beats default):
#   REELDROP_BACKUP_DIR   where archives go             (default: ~/reeldrop-backups)
#   REELDROP_KEEP_DAYS    delete archives older than N days; 0 = never delete
#                         (default: 56, i.e. 8 weekly backups)
#
# The project directory is the parent of this script's folder, so the script
# works from any current directory and from cron.
#
# Cron, every Sunday at 2 AM (install with `crontab -e`; adjust the path):
#   0 2 * * 0  /home/pi/reeldrop/scripts/backup.sh >> /home/pi/reeldrop-backup.log 2>&1
# The original plan used tar directly:
#   0 2 * * 0  tar czf ~/reeldrop-backup-$(date +\%F).tgz -C ~/reeldrop data config
# In a crontab, an unescaped % ends the command, so write \% there. This script
# calls `date` itself, so the crontab line above needs no % at all.
#
# The archive holds secrets (session.json, the database). It is created with
# mode 600 inside a 700 folder; keep copies somewhere equally private.

set -euo pipefail
umask 077

die() { echo "backup: $*" >&2; exit 1; }

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
backup_dir="${1:-${REELDROP_BACKUP_DIR:-$HOME/reeldrop-backups}}"
keep_days="${REELDROP_KEEP_DAYS:-56}"

[[ "$keep_days" =~ ^[0-9]+$ ]] || die "REELDROP_KEEP_DAYS must be a whole number, got '$keep_days'"
[[ -d "$project_dir/data" ]]   || die "no data/ folder in $project_dir"
[[ -d "$project_dir/config" ]] || die "no config/ folder in $project_dir"

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

stamp="$(date +%F)"
final="$backup_dir/reeldrop-backup-$stamp.tgz"
tmp="$(mktemp "$backup_dir/.reeldrop-backup.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

# Write to a temp file first so a failed run never leaves a truncated archive
# under the final name. A second run on the same day replaces that day's file.
tar czf "$tmp" -C "$project_dir" data config
mv -f "$tmp" "$final"
trap - EXIT
echo "backup: wrote $final ($(du -h "$final" | cut -f1))"

if (( keep_days > 0 )); then
  find "$backup_dir" -maxdepth 1 -type f -name 'reeldrop-backup-*.tgz' \
       -mtime "+$keep_days" -print -delete | sed 's/^/backup: pruned /'
fi
