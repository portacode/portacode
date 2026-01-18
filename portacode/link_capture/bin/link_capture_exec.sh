#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
COMMAND_NAME="$1"
shift

if [ -z "$COMMAND_NAME" ]; then
  echo "link_capture: missing command name" >&2
  exit 1
fi

exec "$SCRIPT_DIR/link_capture_wrapper.py" "$COMMAND_NAME" "$@"
