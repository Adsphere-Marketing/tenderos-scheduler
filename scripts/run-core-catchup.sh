#!/usr/bin/env bash
set -euo pipefail

: "${TENDEROS_SCHEDULER_TOKEN:?missing TENDEROS_SCHEDULER_TOKEN}"
: "${TENDEROS_RUN_URL:?missing TENDEROS_RUN_URL}"
: "${TENDEROS_WAKE_URL:?missing TENDEROS_WAKE_URL}"

HEARTBEAT_SECONDS="${TENDEROS_HEARTBEAT_SECONDS:-240}"
MAX_CYCLES="${TENDEROS_CATCHUP_MAX_CYCLES:-8}"
LOCK_RETRIES="${TENDEROS_CATCHUP_LOCK_RETRIES:-4}"

perform_request_with_heartbeat() {
  local response_file="$1"
  local code_file request_pid heartbeat_pid request_exit
  code_file="$(mktemp)"

  curl --silent --show-error \
    --output "$response_file" \
    --write-out "%{http_code}" \
    --max-time 1200 \
    --connect-timeout 30 \
    --retry 2 \
    --retry-delay 20 \
    --retry-all-errors \
    --request POST \
    --header "X-TenderOS-Scheduler-Token: ${TENDEROS_SCHEDULER_TOKEN}" \
    "$TENDEROS_RUN_URL" > "$code_file" &
  request_pid=$!

  (
    while kill -0 "$request_pid" 2>/dev/null; do
      sleep "$HEARTBEAT_SECONDS"
      kill -0 "$request_pid" 2>/dev/null || exit 0
      wake_code="$(
        curl --silent --show-error \
          --output /dev/null \
          --write-out "%{http_code}" \
          --max-time 60 \
          --connect-timeout 20 \
          --retry 2 \
          --retry-delay 5 \
          --retry-all-errors \
          --header "X-TenderOS-Scheduler-Token: ${TENDEROS_SCHEDULER_TOKEN}" \
          "$TENDEROS_WAKE_URL" || true
      )"
      echo "Render keepalive heartbeat: HTTP ${wake_code:-curl-error}"
    done
  ) &
  heartbeat_pid=$!

  if wait "$request_pid"; then
    request_exit=0
  else
    request_exit=$?
  fi

  kill "$heartbeat_pid" 2>/dev/null || true
  wait "$heartbeat_pid" 2>/dev/null || true

  HTTP_CODE="$(cat "$code_file" 2>/dev/null || true)"
  rm -f "$code_file"
  return "$request_exit"
}

previous_pending=""
for cycle in $(seq 1 "$MAX_CYCLES"); do
  response_file="$(mktemp)"
  trap 'rm -f "$response_file"' EXIT

  for lock_attempt in $(seq 1 "$LOCK_RETRIES"); do
    HTTP_CODE=""
    if ! perform_request_with_heartbeat "$response_file"; then
      echo "Core catch-up cycle ${cycle}: request transport failed."
      exit 1
    fi
    if [ "$HTTP_CODE" != "409" ]; then
      break
    fi
    echo "Core catch-up cycle ${cycle}: runtime lock busy (${lock_attempt}/${LOCK_RETRIES})."
    if [ "$lock_attempt" -eq "$LOCK_RETRIES" ]; then
      echo "Another authoritative job owns the lock; catch-up exits safely."
      exit 0
    fi
    sleep 30
  done

  echo "=== TenderOS core catch-up cycle ${cycle}/${MAX_CYCLES} ==="
  cat "$response_file"
  echo

  if [ "$HTTP_CODE" != "200" ]; then
    echo "Core catch-up returned HTTP ${HTTP_CODE}; expected 200."
    exit 1
  fi
  if ! jq -e '.status == "complete" and .mode == "core"' "$response_file" >/dev/null; then
    echo "Core catch-up response did not confirm a completed core cycle."
    exit 1
  fi

  pending="$(jq -r '.pending_notices // -1' "$response_file")"
  if ! [[ "$pending" =~ ^[0-9]+$ ]]; then
    echo "Core catch-up response has invalid pending_notices=${pending}."
    exit 1
  fi

  echo "Core catch-up cycle ${cycle}: pending_notices=${pending}."
  if [ "$pending" -eq 0 ]; then
    echo "TenderOS core catch-up complete: PENDING=0."
    exit 0
  fi

  if [ -n "$previous_pending" ] && [ "$pending" -ge "$previous_pending" ]; then
    echo "Core catch-up stopped: pending_notices did not decrease (${previous_pending} -> ${pending})."
    exit 1
  fi
  previous_pending="$pending"
  rm -f "$response_file"
  trap - EXIT
  sleep 5
done

echo "Core catch-up hit its bounded ${MAX_CYCLES}-cycle cap before PENDING reached zero."
exit 1
