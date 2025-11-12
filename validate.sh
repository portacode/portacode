#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'EOF'
Usage: scripts/reacher_validate.sh [OPTIONS] EMAIL [EMAIL...]

Validate one or more email addresses against the local Reacher service (default http://localhost:8080).

Positional arguments:
  EMAIL                Email address to validate (can provide multiple).

Options:
  --host URL           Reacher base URL (default: $REACHER_HOST or http://localhost:8080).
  --strictness LEVEL   Override strictness (strict|moderate|lenient). Default from REACHER_STRICTNESS or strict.
  -v, --verbose        Output full JSON response for each email in addition to the boolean result.
  -h, --help           Show this help text and exit.

Environment variables:
  REACHER_HOST         Override default host URL for the Reacher API.
  REACHER_STRICTNESS   Set allowed Reacher statuses (strict|moderate|lenient).

EOF
}

HOST="${REACHER_HOST:-http://localhost:8080}"
VERBOSE=false
EMAILS=()
STRICTNESS="${REACHER_STRICTNESS:-strict}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    --host)
      [[ $# -ge 2 ]] || { echo "error: --host requires an argument" >&2; exit 1; }
      HOST="$2"
      shift 2
      ;;
    --strictness)
      [[ $# -ge 2 ]] || { echo "error: --strictness requires an argument" >&2; exit 1; }
      STRICTNESS="$2"
      shift 2
      ;;
    -v|--verbose)
      VERBOSE=true
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "error: unknown option $1" >&2
      exit 1
      ;;
    *)
      EMAILS+=("$1")
      shift
      ;;
  esac
done

EMAILS+=("$@")

if [[ ${#EMAILS[@]} -eq 0 ]]; then
  echo "error: EMAIL is required" >&2
  show_help >&2
  exit 1
fi

allowed_statuses() {
  case "${1,,}" in
    moderate)
      echo "safe risky"
      ;;
    lenient)
      echo "safe risky unknown"
      ;;
    *)
      echo "safe"
      ;;
  esac
}

ALLOWED_STATUSES=" $(allowed_statuses "$STRICTNESS") "

parse_is_reachable() {
  local response="$1"
  if command -v jq >/dev/null 2>&1; then
    echo "$response" | jq -r '.is_reachable // empty'
    return
  fi
  local python_cmd=""
  if command -v python3 >/dev/null 2>&1; then
    python_cmd="python3"
  elif command -v python >/dev/null 2>&1; then
    python_cmd="python"
  fi
  if [[ -n "$python_cmd" ]]; then
    echo "$response" | "$python_cmd" -c 'import json, sys; print(json.load(sys.stdin).get("is_reachable",""))'
  else
    echo ""
  fi
}

pretty_print() {
  local response="$1"
  if command -v jq >/dev/null 2>&1; then
    echo "$response" | jq .
    return
  fi
  local python_cmd=""
  if command -v python3 >/dev/null 2>&1; then
    python_cmd="python3"
  elif command -v python >/dev/null 2>&1; then
    python_cmd="python"
  fi
  if [[ -n "$python_cmd" ]]; then
    echo "$response" | "$python_cmd" -m json.tool
  else
    echo "$response"
  fi
}

for TO_EMAIL in "${EMAILS[@]}"; do
  RESPONSE=$(curl -sS -X POST "$HOST/v0/check_email" \
    -H 'Content-Type: application/json' \
    -d "{\"to_email\": \"${TO_EMAIL}\"}")

  IS_REACHABLE=$(parse_is_reachable "$RESPONSE")
  IS_REACHABLE=$(echo -n "$IS_REACHABLE" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  if [[ -n "$IS_REACHABLE" && "$ALLOWED_STATUSES" == *" $IS_REACHABLE "* ]]; then
    RESULT="True"
  else
    RESULT="False"
  fi

  echo "$TO_EMAIL: $RESULT"

  if $VERBOSE; then
    pretty_print "$RESPONSE"
  fi
done
