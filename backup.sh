# backup.sh
#!/usr/bin/env bash
set -euo pipefail

# ensure we’re in project root
cd "$(dirname "$0")"

# config
BACKUP_DIR="${1:-$PWD/../backups}"
VOLUME_NAME="portacode_pgdata"
SERVICE_NAME="db"

mkdir -p "$BACKUP_DIR"

# check & stop DB for a consistent dump
if [ "$(docker inspect -f '{{.State.Running}}' portacode-db 2>/dev/null || echo false)" = "true" ]; then
  echo "🛑 Stopping $SERVICE_NAME..."
  docker-compose stop "$SERVICE_NAME"
  RESTART_DB=true
else
  RESTART_DB=false
fi

# create backup
TS=$(date +%F_%H-%M-%S)
FILE="pgdata-$TS.tar.gz"
echo "📦 Backing up volume to $BACKUP_DIR/$FILE..."
docker run --rm \
  -v "${VOLUME_NAME}":/data:ro \
  -v "${BACKUP_DIR}":/backup \
  alpine \
  sh -c "cd /data && tar czf /backup/${FILE} ."

echo "✅ Backup complete."

# restart DB if we stopped it
if [ "$RESTART_DB" = true ]; then
  echo "🔄 Starting $SERVICE_NAME..."
  docker-compose start "$SERVICE_NAME"
fi

echo "🎉 Done."
