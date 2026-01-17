#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

#######################################
# load .env
#######################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  . "${SCRIPT_DIR}/.env"
  set +a
fi

if [ -n "${TZ:-}" ] && [ -e "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "${TZ}" > /etc/timezone
fi

: "${BASE_URL:?BASE_URL not set}"
: "${API_KEY:?API_KEY not set}"
: "${KNOWLEDGE_ID:?KNOWLEDGE_ID not set}"
: "${WORKERS:=4}"
: "${MAX_RETRIES:=5}"
: "${STATUS_POLL_INTERVAL:=2}"
: "${SYNC_INTERVAL:=300}"

WATCH_DIR="/inbox"
MAP_FILE="${SCRIPT_DIR}/knowledge-map.txt"
QUEUE_DIR="${SCRIPT_DIR}/queue"
INFLIGHT_DIR="${SCRIPT_DIR}/inflight"

log() {
  printf '[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*" >&2
}

is_ignored_name() {
  local name="$1"
  [[ "$name" = .* || "$name" == *.swp || "$name" == *.tmp || "$name" == *~ ]]
}

api_get() {
  local url="$1"
  curl -sS \
    -H "Authorization: Bearer $API_KEY" \
    "$BASE_URL$url"
}

api_post_json() {
  local url="$1"
  local json="$2"
  curl -sS \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -X POST \
    -d "$json" \
    "$BASE_URL$url"
}

ensure_map_file() {
  mkdir -p "$(dirname "$MAP_FILE")"
  touch "$MAP_FILE"
}

init_queue() {
  mkdir -p "$QUEUE_DIR" "$INFLIGHT_DIR"
  for job in "$INFLIGHT_DIR"/*; do
    mv "$job" "$QUEUE_DIR/" 2>/dev/null || true
  done
}

is_queued() {
  local relpath="$1"
  local files=("$QUEUE_DIR"/* "$INFLIGHT_DIR"/*)
  [ ${#files[@]} -eq 0 ] && return 1
  grep -F -m1 -- "$relpath|" "${files[@]}" >/dev/null 2>&1
}

enqueue_file() {
  local path="$1"
  local relpath="${path#$WATCH_DIR/}"

  is_queued "$relpath" && return 0

  local job_id
  job_id="$(date +%s%N)-$$-$RANDOM"
  printf '%s|0\n' "$relpath" > "${QUEUE_DIR}/${job_id}"
}

remove_from_queue() {
  local relpath="$1"
  local job
  for job in "$QUEUE_DIR"/* "$INFLIGHT_DIR"/*; do
    [ -e "$job" ] || continue
    if grep -F -m1 -- "$relpath|" "$job" >/dev/null 2>&1; then
      rm -f "$job"
    fi
  done
}

pick_job() {
  local job base
  for job in "$QUEUE_DIR"/*; do
    [ -e "$job" ] || return 1
    base="$(basename "$job")"
    if mv "$job" "$INFLIGHT_DIR/$base" 2>/dev/null; then
      echo "$INFLIGHT_DIR/$base"
      return 0
    fi
  done
  return 1
}

process_job() {
  local job_path="$1"
  local relpath attempts
  IFS='|' read -r relpath attempts < "$job_path"
  attempts="${attempts:-0}"

  local fullpath="${WATCH_DIR}/${relpath}"
  if [ ! -f "$fullpath" ]; then
    log "queue: missing file, skip $relpath"
    rm -f "$job_path"
    return 0
  fi

  if upload_file "$fullpath"; then
    rm -f "$job_path"
    return 0
  fi

  if [ "$attempts" -lt "$MAX_RETRIES" ]; then
    local backoff=$((1 << attempts))
    log "queue: retry $relpath in ${backoff}s (attempt $((attempts + 1))/$MAX_RETRIES)"
    sleep "$backoff"
    printf '%s|%s\n' "$relpath" "$((attempts + 1))" > "$job_path"
    mv "$job_path" "$QUEUE_DIR/" 2>/dev/null || true
    return 0
  fi

  log "queue: giving up on $relpath after $MAX_RETRIES attempts"
  rm -f "$job_path"
  return 1
}

worker_loop() {
  while true; do
    local job_path
    job_path="$(pick_job)" || { sleep 0.2; continue; }
    process_job "$job_path" || true
  done
}

save_file_id() {
  local relpath="$1"
  local file_id="$2"

  ensure_map_file
  grep -v "^$relpath|" "$MAP_FILE" > "${MAP_FILE}.tmp" || true
  mv "${MAP_FILE}.tmp" "$MAP_FILE"
  echo "$relpath|$file_id" >> "$MAP_FILE"
}

load_file_id() {
  local relpath="$1"
  ensure_map_file
  grep "^$relpath|" "$MAP_FILE" | tail -n1 | cut -d'|' -f2 || true
}

remove_mapping() {
  local relpath="$1"
  ensure_map_file
  grep -v "^$relpath|" "$MAP_FILE" > "${MAP_FILE}.tmp" || true
  mv "${MAP_FILE}.tmp" "$MAP_FILE"
}

wait_for_file_ready() {
  local file_id="$1"
  local name="$2"

  while true; do
    local resp status
    resp="$(api_get "/api/v1/files/${file_id}/process/status")" || {
      log "ERROR: status request failed for $name ($file_id)"
      return 1
    }
    status="$(echo "$resp" | jq -r '.status // empty')"

    case "$status" in
      completed)
        log "status $name: completed"
        return 0
        ;;
      failed)
        log "ERROR: processing failed for $name ($file_id): $resp"
        return 1
        ;;
      ""|null)
        log "WARN: no status in response for $name ($file_id): $resp"
        ;;
      *)
        log "status $name: $status"
        ;;
    esac

    sleep "$STATUS_POLL_INTERVAL"
  done
}

add_file_to_knowledge() {
  local file_id="$1"
  local name="$2"

  local resp
  resp="$(api_post_json "/api/v1/knowledge/${KNOWLEDGE_ID}/file/add" "{\"file_id\":\"${file_id}\"}")" || {
    log "ERROR: file/add failed for $name ($file_id)"
    log "resp: $resp"
    return 1
  }

  log "file/add OK for $name"
}

remove_file_from_knowledge() {
  local name="$1"
  local file_id="$2"

  if [ -z "$file_id" ]; then
    file_id="$(resolve_file_id_by_name "$name")"
  fi

  if [ -z "$file_id" ]; then
    log "WARN: no FILE_ID found for $name, skipping remove"
    return 0
  fi

  log "cleanup in OpenWebUI for $name ($file_id)"

  local resp
  resp="$(api_post_json "/api/v1/knowledge/${KNOWLEDGE_ID}/file/remove" "{\"file_id\":\"${file_id}\"}")" || {
    log "ERROR: file/remove request failed for $name ($file_id)"
    log "resp: $resp"
    return 1
  }

  log "file/remove response for $name: $resp"
}

resolve_file_id_by_name() {
  local name="$1"
  local resp
  resp="$(api_get "/api/v1/knowledge/${KNOWLEDGE_ID}")" || {
    log "ERROR: knowledge lookup failed for $name"
    return 1
  }
  echo "$resp" | jq -r --arg name "$name" '
    .files[]? | select(.meta.name == $name) | .id
  ' | head -n1
}

periodic_sync() {
  log "sync: start"

  ensure_map_file

  declare -A local_names
  declare -A local_paths
  declare -A knowledge_ids
  declare -A knowledge_counts

  while IFS= read -r -d '' path; do
    local relpath name
    relpath="${path#$WATCH_DIR/}"
    name="$(basename "$path")"
    if is_ignored_name "$name"; then
      continue
    fi
    local_names["$name"]=$(( ${local_names["$name"]:-0} + 1 ))
    local_paths["$relpath"]=1
    if ! load_file_id "$relpath" >/dev/null; then
      enqueue_file "$path"
    fi
  done < <(find "$WATCH_DIR" -type f -print0)

  local resp
  resp="$(api_get "/api/v1/knowledge/${KNOWLEDGE_ID}")" || {
    log "ERROR: sync failed to list knowledge files"
    return 1
  }

  while IFS='|' read -r name file_id; do
    [ -z "$name" ] && continue
    knowledge_counts["$name"]=$(( ${knowledge_counts["$name"]:-0} + 1 ))
    knowledge_ids["$name"]="$file_id"
  done < <(echo "$resp" | jq -r '.files[]? | "\(.meta.name)|\(.id)"')

  while IFS='|' read -r relpath file_id; do
    [ -z "$relpath" ] && continue
    if [ -z "${local_paths["$relpath"]+x}" ]; then
      remove_file_from_knowledge "$relpath" "$file_id" || log "WARN: remove failed for $relpath ($file_id)"
      remove_mapping "$relpath"
    fi
  done < "$MAP_FILE"

  for name in "${!knowledge_ids[@]}"; do
    if [ "${local_names["$name"]:-0}" -eq 0 ] && [ "${knowledge_counts["$name"]:-0}" -eq 1 ]; then
      remove_file_from_knowledge "$name" "${knowledge_ids["$name"]}" || log "WARN: remove failed for $name"
    fi
  done

  log "sync: done"
}

periodic_sync_loop() {
  if [ "$SYNC_INTERVAL" -le 0 ]; then
    log "sync: disabled"
    return 0
  fi

  while true; do
    sleep "$SYNC_INTERVAL"
    periodic_sync || true
  done
}

upload_file() {
  local path="$1"
  local relpath="${path#$WATCH_DIR/}"
  local name
  name="$(basename "$path")"

  local existing_id
  existing_id="$(load_file_id "$relpath")"
  if [ -n "$existing_id" ]; then
    log "skip upload, already known: $relpath ($existing_id)"
    return 0
  fi

  log "upload: $relpath"

  local resp file_id
  resp="$(curl -sS \
    -H "Authorization: Bearer $API_KEY" \
    -H "Accept: application/json" \
    -F "file=@${path}" \
    "$BASE_URL/api/v1/files/")" || {
      log "ERROR: upload failed for $relpath"
      log "resp: $resp"
      return 1
    }

  file_id="$(echo "$resp" | jq -r '.id // empty')"
  if [ -z "$file_id" ] || [ "$file_id" = "null" ]; then
    log "ERROR: no FILE_ID returned for $relpath: $resp"
    return 1
  fi

  log "FILE_ID for $relpath: $file_id"

  wait_for_file_ready "$file_id" "$relpath" || return 1
  add_file_to_knowledge "$file_id" "$relpath" || return 1

  save_file_id "$relpath" "$file_id"
}

startup_cleanup() {
  ensure_map_file
  log "startup cleanup: sync $WATCH_DIR ↔ knowledge-map"

  while IFS='|' read -r relpath file_id; do
    [ -z "$relpath" ] && continue

    if [ ! -f "${WATCH_DIR}/${relpath}" ]; then
      log "startup: ${relpath} is missing locally, removing from knowledge"
      if [ -n "$file_id" ]; then
        remove_file_from_knowledge "$relpath" "$file_id" || log "WARN: remove failed for $relpath ($file_id)"
      fi
      remove_mapping "$relpath"
    fi
  done < "$MAP_FILE"
}

watch_loop() {
  log "BASE_URL: $BASE_URL"
  log "watching (recursive): $WATCH_DIR"

  inotifywait -m -r -q -e CREATE,MOVED_TO,DELETE,MOVED_FROM --format '%e %w%f' "$WATCH_DIR" | \
  while read -r events fullpath; do
    # ignore folders (not their content)
    if [ -d "$fullpath" ]; then
      log "ignoring: $fullpath (event: $events)"
      continue
    fi

    local relpath name
    relpath="${fullpath#$WATCH_DIR/}"
    name="$(basename "$fullpath")"

    # ignore tmp and swp files
    if is_ignored_name "$name"; then
      log "ignoring tmp file: $relpath (event: $events)"
      continue
    fi

    case "$events" in
      *CREATE*|*MOVED_TO*)
        sleep 1
        if [ -f "$fullpath" ]; then
          enqueue_file "$fullpath"
        else
          log "CREATE/MOVED_TO but file doesnt exist: $fullpath"
        fi
        ;;
      *DELETE*|*MOVED_FROM*)
        local file_id
        file_id="$(load_file_id "$relpath")"
        remove_file_from_knowledge "$relpath" "$file_id" || log "WARN: remove failed for $relpath ($file_id)"
        if [ -n "$file_id" ]; then
          remove_mapping "$relpath"
        fi
        remove_from_queue "$relpath"
        ;;
      *)
        log "ignoring event $events for $fullpath"
        ;;
    esac
  done
}

command -v inotifywait >/dev/null 2>&1 || {
  log "ERROR: inotifywait not found!"
  exit 1
}

command -v jq >/dev/null 2>&1 || {
  log "ERROR: jq not found!"
  exit 1
}

[ -d "$WATCH_DIR" ] || {
  log "ERROR: $WATCH_DIR does not exist; mount your host folder to /inbox"
  exit 1
}

startup_cleanup
init_queue
for _ in $(seq 1 "$WORKERS"); do
  worker_loop &
done
periodic_sync_loop &
watch_loop
