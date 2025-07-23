#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# ─── CONFIG ────────────────────────────────────────────────────────────────
BACKUP_DIR="${1:-$PWD/../backups}"
VOLUME_NAME="portacode_pgdata"
SERVICE="db"

# ─── PICK A BACKUP ────────────────────────────────────────────────────────
mapfile -t BACKUPS < <(
  find "$BACKUP_DIR" -maxdepth 1 -type f -name "pgdata-*.tar.gz" \
    -printf "%f\n" | sort -r
)
[ ${#BACKUPS[@]} -gt 0 ] || { echo "❌ No backups in $BACKUP_DIR"; exit 1; }

echo "Available backups:"
for i in "${!BACKUPS[@]}"; do
  printf "  %2d) %s\n" $((i+1)) "${BACKUPS[i]}"
done
read -rp "Select backup [1-${#BACKUPS[@]}]: " SEL
(( SEL>=1 && SEL<=${#BACKUPS[@]} )) || { echo "❌ Invalid choice."; exit 1; }
FILE="${BACKUPS[$((SEL-1))]}"

# ─── STOP & REMOVE OLD DB ─────────────────────────────────────────────────
if docker-compose ps --status=running | grep -q "$SERVICE"; then
  echo "🛑 Stopping & removing existing '$SERVICE' container..."
  docker-compose stop "$SERVICE"
  docker-compose rm -f "$SERVICE"
fi

# ─── DROP THE OLD VOLUME ───────────────────────────────────────────────────
if docker volume inspect "$VOLUME_NAME" &>/dev/null; then
  echo "🗑️  Removing old volume $VOLUME_NAME..."
  docker volume rm "$VOLUME_NAME"
fi

# ─── HAVE COMPOSE CREATE THE BLANK VOLUME ─────────────────────────────────
echo "➕ Letting Docker Compose create a fresh volume for '$SERVICE'..."
# 'create' makes the container (and volume) without starting it
docker-compose create "$SERVICE"

# ─── RESTORE INTO THAT VOLUME ─────────────────────────────────────────────
echo "📥 Restoring '$FILE' into volume '$VOLUME_NAME'..."
docker run --rm \
  -v "${VOLUME_NAME}":/data \
  -v "$BACKUP_DIR":/backup \
  alpine \
  sh -c "cd /data && tar xzf /backup/${FILE}"

# ─── START THE DB AGAIN ────────────────────────────────────────────────────
echo "🚀 Starting '$SERVICE'..."
docker-compose up -d "$SERVICE"

echo "✅ Restore complete."
