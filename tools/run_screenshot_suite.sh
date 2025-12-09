#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/test_results/screenshot_runs"
STATIC_DIR="$ROOT_DIR/server/portacode_django/static/images/marketing"

ENV_FILE="$ROOT_DIR/.env.play_store"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
else
    echo "Missing $ENV_FILE with SCREENSHOT_USERNAME/SCREENSHOT_PASSWORD"
    exit 1
fi

: "${SCREENSHOT_USERNAME:?SCREENSHOT_USERNAME missing in $ENV_FILE}"
: "${SCREENSHOT_PASSWORD:?SCREENSHOT_PASSWORD missing in $ENV_FILE}"

echo "🧹 Resetting test_results directory..."
rm -rf "$ROOT_DIR/test_results"
mkdir -p "$OUT_DIR"
echo "🧽 Refreshing marketing static assets..."
rm -rf "$STATIC_DIR"
mkdir -p "$STATIC_DIR"

run_profile() {
    local profile_name=$1
    local test_name=$2
    shift
    shift
    echo ""
    echo "=== Running screenshot profile: ${profile_name} ==="

    env \
        TEST_USERNAME="$SCREENSHOT_USERNAME" \
        TEST_PASSWORD="$SCREENSHOT_PASSWORD" \
        SCREENSHOT_DEVICE_NAME="$profile_name" \
        ALLOW_EMPTY_SESSIONS=true \
        "$@" \
        python -m testing_framework.cli run-tests "$test_name"

    local latest_run
    latest_run=$(ls -dt "$ROOT_DIR"/test_results/run_* | head -n 1)
    local recording_dir
    recording_dir=$(ls -d "$latest_run"/recordings/shared_session_* | head -n 1)
    local profile_dir="$OUT_DIR/${profile_name}"
    rm -rf "$profile_dir"
    mkdir -p "$profile_dir"
    cp "$recording_dir"/screenshots/*.png "$profile_dir"/
    echo "Stored screenshots for ${profile_name} in $profile_dir"

    local static_target="$STATIC_DIR/${profile_name}"
    rm -rf "$static_target"
    mkdir -p "$static_target"
    cp "$profile_dir"/*.png "$static_target"/
    echo "Synced ${profile_name} screenshots to $static_target"
}

GALAXY_UA="Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S908U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36"

run_profile phone_s22_ultra play_store_phone_screenshot_test \
    TEST_VIEWPORT_WIDTH=384 \
    TEST_VIEWPORT_HEIGHT=844 \
    TEST_DEVICE_SCALE_FACTOR=3.125 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_USER_AGENT="$GALAXY_UA" \
    TEST_VIDEO_WIDTH=1288 \
    TEST_VIDEO_HEIGHT=2859 \
    SCREENSHOT_ZOOM=0.9

run_profile tablet_7_inch play_store_tablet_screenshot_test \
    TEST_VIEWPORT_WIDTH=960 \
    TEST_VIEWPORT_HEIGHT=600 \
    TEST_DEVICE_SCALE_FACTOR=2.0 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_VIDEO_WIDTH=1200 \
    TEST_VIDEO_HEIGHT=1920 \
    SCREENSHOT_ZOOM=1.0

run_profile tablet_10_inch play_store_tablet_screenshot_test \
    TEST_VIEWPORT_WIDTH=1280 \
    TEST_VIEWPORT_HEIGHT=800 \
    TEST_DEVICE_SCALE_FACTOR=2.0 \
    TEST_IS_MOBILE=true \
    TEST_HAS_TOUCH=true \
    TEST_VIDEO_WIDTH=1600 \
    TEST_VIDEO_HEIGHT=2560 \
    SCREENSHOT_ZOOM=1.0
