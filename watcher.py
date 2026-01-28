#!/usr/bin/env python3
import os
import time
import requests
from datetime import datetime


def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


class Syncer:
    def __init__(self, base_url, api_key, knowledge_id, watch_dir="/inbox"):
        # Ensure scheme for requests (e.g., http://openwebui:8080)
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        self.base_url = base_url.rstrip("/")
        self.knowledge_id = knowledge_id
        self.watch_dir = watch_dir
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    # ----- local -----
    def get_local_files(self) -> dict[str, str]:
        files = {}
        for root, _, filenames in os.walk(self.watch_dir):
            for name in filenames:
                if name.startswith(".") or name.endswith((".swp", ".tmp", "~")):
                    continue
                full = os.path.join(root, name)
                if os.path.getsize(full) == 0:
                    continue
                rel = os.path.relpath(full, self.watch_dir)
                files[rel] = full
        return files

    # ----- remote -----
    def get_remote_files(self) -> dict[str, str]:
        resp = self.session.get(
            f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/files",
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict):
            files = payload.get("items") or payload.get("files") or payload.get("data") or []
        else:
            files = payload
        out = {}
        for item in files:
            meta = item.get("meta") or {}
            name = meta.get("name") or item.get("filename")
            file_id = item.get("id")
            if name and file_id:
                out[name] = file_id
        return out

    def upload_file(self, fullpath: str, display_name: str) -> str:
        with open(fullpath, "rb") as handle:
            resp = self.session.post(
                f"{self.base_url}/api/v1/files/",
                files={"file": (display_name, handle)},
                timeout=120,
            )
        resp.raise_for_status()
        file_id = resp.json().get("id")
        if not file_id:
            raise RuntimeError(f"no FILE_ID returned for {display_name}: {resp.text}")
        return file_id

    def add_to_knowledge(self, file_id: str, name: str) -> bool:
        url = f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/file/add"
        resp = self.session.post(
            url,
            json={"file_id": file_id, "metadatas": [], "metadata": {}},
            timeout=30,
        )
        if resp.status_code == 400:
            # Some OWUI versions expect file_ids instead of file_id.
            resp = self.session.post(url, json={"file_ids": [file_id]}, timeout=30)
            if resp.status_code == 400:
                log(f"add failed for {name}: {resp.status_code} {resp.text}")
                return False
        resp.raise_for_status()
        return True

    def delete_remote(self, file_id: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/file/remove",
            json={"file_id": file_id},
            timeout=30,
        )
        resp.raise_for_status()

    # ----- sync -----
    def sync_once(self) -> None:
        local = self.get_local_files()
        remote = self.get_remote_files()

        local_names = {os.path.basename(p) for p in local.keys()}
        to_upload = [(os.path.basename(rel), full) for rel, full in local.items() if os.path.basename(rel) not in remote]
        to_delete = [(name, file_id) for name, file_id in remote.items() if name not in local_names]

        if not to_upload and not to_delete:
            log(f"sync: no changes (local {len(local_names)}, remote {len(remote)})")
            return
        add_failed = 0
        for name, full in to_upload:
            file_id = self.upload_file(full, name)
            if not self.add_to_knowledge(file_id, name):
                add_failed += 1

        for name, file_id in to_delete:
            self.delete_remote(file_id)

        log(
            f"sync: {len(to_upload)} upload(s), {add_failed} add_failed, {len(to_delete)} delete(s) "
            f"(local {len(local_names)}, remote {len(remote)})"
        )

    def run(self, interval_seconds: int = 5) -> None:
        log(f"start polling every {interval_seconds}s")
        try:
            self.sync_once()
            while True:
                time.sleep(interval_seconds)
                self.sync_once()
        except KeyboardInterrupt:
            log("stopped")


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} not set")
    return value


if __name__ == "__main__":
    base_url = env_required("BASE_URL")
    api_key = env_required("API_KEY")
    knowledge_id = env_required("KNOWLEDGE_ID")
    watch_dir = "/inbox"
    interval = int(os.getenv("SYNC_INTERVAL", "10"))

    log("starting watcher")
    Syncer(base_url, api_key, knowledge_id, watch_dir).run(interval)
