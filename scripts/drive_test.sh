#!/usr/bin/env bash
# Live Google Drive sync checks against a SEPARATE test folder, in an ISOLATED Docker Compose project.
#
#   TEST_FOLDER_ID=<id> scripts/drive_test.sh sync      # one synchronisation, per-file report
#   TEST_FOLDER_ID=<id> scripts/drive_test.sh docs      # documents and statuses
#   TEST_FOLDER_ID=<id> scripts/drive_test.sh pending   # exactly what /pending shows
#   TEST_FOLDER_ID=<id> scripts/drive_test.sh search "query" pid_dubom
#   TEST_FOLDER_ID=<id> scripts/drive_test.sh failing-sync   # listing with an invalid key: must change nothing
#   scripts/drive_test.sh reset                          # delete ONLY the isolated test volume
#
# Isolation: project name "forest-drive-test" -> its own volume (forest-drive-test_forest-data).
# The production volume, .env folder setting and the running Telegram bot are never touched;
# these commands do not start Telegram polling.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=forest-drive-test
if [[ "${1:-}" == "reset" ]]; then
  docker volume rm "${PROJECT}_forest-data" && echo "isolated test volume removed"; exit 0
fi
: "${TEST_FOLDER_ID:?set TEST_FOLDER_ID to the test folder id}"
if [[ "$TEST_FOLDER_ID" == "1b_hEjnkQfP8ZVP-OEbnYWqoGSFwXMAP3" ]]; then
  echo "refusing: that is the original archive"; exit 2
fi
run() { docker compose -p "$PROJECT" run --rm -e GOOGLE_DRIVE_FOLDER_ID="$TEST_FOLDER_ID" bot python -m forest_bot "$@"; }
case "${1:-}" in
  sync)          run sync 2>&1 | grep -vE "^ ?(Container|Network|Volume)" ;;
  docs|pending)  run "$1" 2>&1 | grep -vE "^ ?(Container|Network|Volume)|INFO|WARNING" ;;
  search)        docker compose -p "$PROJECT" run --rm -e GOOGLE_DRIVE_FOLDER_ID="$TEST_FOLDER_ID" bot \
                   python -m forest_bot search "$2" "$3" 2>&1 | grep -vE "^ ?(Container|Network|Volume)|INFO|WARNING" ;;
  failing-sync)  docker compose -p "$PROJECT" run --rm -e GOOGLE_DRIVE_FOLDER_ID="$TEST_FOLDER_ID" \
                   -e GOOGLE_DRIVE_API_KEY=invalid-key-for-test bot python -m forest_bot sync 2>&1 \
                   | grep -vE "^ ?(Container|Network|Volume)" | tail -3 ;;
  *) sed -n 2,14p "$0"; exit 2 ;;
esac
