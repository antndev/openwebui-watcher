# OWUI-Watcher (Python)

Minimal Python watcher that one-way mirrors files from `/inbox` to an OpenWebUI knowledge base.
No web UI. Polling-based for reliability on Docker Desktop mounts.

Behavior:
- Recursive scan of `WATCH_DIR` (default `/inbox`).
- Ignores dotfiles, `*.swp`, `*.tmp`, `*~`, and folders named `ignore`.
- One-way mirror: uploads files missing on the knowledge base.
- Deletes knowledge entries when local files disappear.
- Files rejected by OpenWebUI (e.g. unsupported format) are moved into `WATCH_DIR/FAILED_DIR_NAME`
  with a sidecar error JSON file and are ignored by sync.
- Runs a full mirror pass on a fixed interval.
- Logs progress and ETA during uploads, adds, and deletions.

Env vars:
| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `BASE_URL` | yes | - | OpenWebUI base URL, e.g. `http://host:3000` |
| `API_KEY` | yes | - | OpenWebUI API key |
| `KNOWLEDGE_ID` | yes | - | Knowledge base ID |
| `WATCH_DIR` | no | `/inbox` | Folder to scan recursively |
| `SYNC_INTERVAL` | no | `10` | Seconds between full sync scans |
| `STATE_PATH` | no | `/tmp/openwebui-watcher-state.json` | Local state file for resumable sync |
| `STABLE_AGE_SECONDS` | no | `10` | File must be unchanged for this many seconds before upload |
| `FAILED_DIR_NAME` | no | `_upload_failed` | Subfolder in `WATCH_DIR` for quarantined/rejected files |
| `STATUS_EVERY` | no | `10` | Log progress every N operations |
| `PROGRESS_BYTES` | no | `26214400` | Log upload progress every N bytes |
| `REQUEST_TIMEOUT` | no | `60` | Timeout for non-upload API calls (seconds) |
| `UPLOAD_TIMEOUT` | no | `300` | Timeout for upload API calls (seconds) |
| `REQUEST_RETRIES` | no | `3` | Retries for transient HTTP errors |
| `REQUEST_BACKOFF_SECONDS` | no | `1.0` | Base backoff between retries |

Example:
```bash
docker build -t openwebui-watcher:local .
docker run --rm \
  -e BASE_URL="http://your-openwebui:3000" \
  -e API_KEY="your-key" \
  -e KNOWLEDGE_ID="your-knowledge-id" \
  -e SYNC_INTERVAL=10 \
  -v "/srv/inbox:/inbox" \
  openwebui-watcher:local
```
