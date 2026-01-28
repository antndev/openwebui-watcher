# OWUI-Watcher (Python)

Minimal Python watcher that one-way mirrors files from `/inbox` to an OpenWebUI knowledge base.
No web UI. Polling-based for reliability on Docker Desktop mounts.

Behavior:
- Recursive scan of `/inbox` (volume mount only; not configurable via env).
- Ignores dotfiles, `*.swp`, `*.tmp`, `*~`.
- One-way mirror: uploads files missing on the knowledge base.
- Deletes knowledge entries when local files disappear.
- Runs a full mirror pass on a fixed interval.

Env vars:
| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `BASE_URL` | yes | - | OpenWebUI base URL, e.g. `http://host:3000` |
| `API_KEY` | yes | - | OpenWebUI API key |
| `KNOWLEDGE_ID` | yes | - | Knowledge base ID |
| `SYNC_INTERVAL` | no | `10` | Seconds between full sync scans |

Example:
```bash
docker build -t openwebui-watcher:local .
docker run --rm \
  -e BASE_URL="http://your-openwebui:3000" \
  -e API_KEY="your-key" \
  -e KNOWLEDGE_ID="your-knowledge-id" \
  -e SYNC_INTERVAL=10 \
  -v "/srv/inbox:/inbox:ro" \
  openwebui-watcher:local
```
