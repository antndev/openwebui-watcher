# OWUI-Watcher

Watches `/inbox` inside the container and automatically syncs files to an OpenWebUI knowledge base.

Best practice is to mount whatever host directory you want to watch into `/inbox`.

Behavior:
- Recursive watch: all subfolders are included; folder structure is only used for deduplication and cleanup.
- Ignore rules: dotfiles, `*.swp`, `*.tmp`, and `*~` are skipped.
- Sync rules: on startup, missing local files are removed from the knowledge base; deletes/moves are mirrored.
- Queueing: uploads are queued, parallelized, and retried with backoff; queue state is persisted on disk.
- Status polling: waits for OpenWebUI processing to finish before adding to the knowledge base.

Env vars:
| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `BASE_URL` | yes | - | OpenWebUI base URL, e.g. `http://host:3000` |
| `API_KEY` | yes | - | OpenWebUI API key |
| `KNOWLEDGE_ID` | yes | - | Knowledge base ID |
| `WORKERS` | no | `4` | Parallel upload workers |
| `MAX_RETRIES` | no | `5` | Upload retry attempts |
| `STATUS_POLL_INTERVAL` | no | `2` | Seconds between status checks |
| `TZ` | no | `UTC` | Timezone (used for log timestamps) |

Example:
```bash
docker run --rm \
  -e BASE_URL="http://your-openwebui:3000" \
  -e API_KEY="your-key" \
  -e KNOWLEDGE_ID="your-knowledge-id" \
  -e TZ="Europe/Berlin" \
  -e WORKERS=4 \
  -e MAX_RETRIES=5 \
  -e STATUS_POLL_INTERVAL=2 \
  -v "/srv/inbox:/inbox:ro" \
  openwebui-watcher:latest
```
