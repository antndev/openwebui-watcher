#!/usr/bin/env bash
set -euo pipefail

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
: "${WATCH_DIR:?WATCH_DIR not set}"

MAP_FILE="${SCRIPT_DIR}/knowledge-map.txt"

log() {
  printf '[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*" >&2
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

    sleep 2
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

  log "cleanup in OpenWebUI for $name ($file_id)"

  local resp
  resp="$(api_post_json "/api/v1/knowledge/${KNOWLEDGE_ID}/file/remove" "{\"file_id\":\"${file_id}\"}")" || {
    log "ERROR: file/remove request failed for $name ($file_id)"
    log "resp: $resp"
    return 1
  }

  log "file/remove response for $name: $resp"
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

  inotifywait -m -r -e CREATE,MOVED_TO,DELETE,MOVED_FROM --format '%e %w%f' "$WATCH_DIR" | \
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
    if [[ "$name" = .* || "$name" == *.swp || "$name" == *.tmp || "$name" == *~ ]]; then
      log "ignoring tmp file: $relpath (event: $events)"
      continue
    fi

    case "$events" in
      *CREATE*|*MOVED_TO*)
        sleep 1
        if [ -f "$fullpath" ]; then
          upload_file "$fullpath"
        else
          log "CREATE/MOVED_TO but file doesnt exist: $fullpath"
        fi
        ;;
      *DELETE*|*MOVED_FROM*)
        local file_id
        file_id="$(load_file_id "$relpath")"
        if [ -n "$file_id" ]; then
          remove_file_from_knowledge "$relpath" "$file_id" || log "WARN: remove failed for $relpath ($file_id)"
          remove_mapping "$relpath"
        else
          log "no FILE_ID found in map for $relpath, nothing to delete"
        fi
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

startup_cleanup
watch_loop
