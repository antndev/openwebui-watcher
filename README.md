# OWUI-Watcher (Python)

Minimal Python watcher that syncs files from `/inbox` to an OpenWebUI knowledge base.
No web UI. Polling-based for reliability on Docker Desktop mounts.

Behavior:
- Recursive scan of `/inbox`.
- Ignores dotfiles, `*.swp`, `*.tmp`, `*~`.
- Uploads missing files, waits for processing, then adds to knowledge base.
- Removes knowledge entries when local files disappear.
- Runs a full sync on a fixed interval.

Env vars:
| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `BASE_URL` | yes | - | OpenWebUI base URL, e.g. `http://host:3000` |
| `API_KEY` | yes | - | OpenWebUI API key |
| `KNOWLEDGE_ID` | yes | - | Knowledge base ID |
| `WATCH_DIR` | no | `/inbox` | Directory to watch |
| `DATA_DIR` | no | `.` | Map storage directory |
| `STATUS_POLL_INTERVAL` | no | `1` | Seconds between status checks |
| `SYNC_INTERVAL` | no | `5` | Seconds between full sync scans |
| `INTERVAL_SECONDS` | no | `5` | Alias for `SYNC_INTERVAL` |
| `MAX_RETRIES` | no | `3` | Upload retry attempts |

Example:
```bash
docker build -t openwebui-watcher:local .
docker run --rm \
  -e BASE_URL="http://your-openwebui:3000" \
  -e API_KEY="your-key" \
  -e KNOWLEDGE_ID="your-knowledge-id" \
  -e SYNC_INTERVAL=5 \
  -v "/srv/inbox:/inbox:ro" \
  openwebui-watcher:local
```
