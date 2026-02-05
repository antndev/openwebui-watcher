#!/usr/bin/env python3
import os
import json
import shutil
import time
import requests
import signal
from datetime import datetime
from typing import Callable


def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def format_bytes(count: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(count)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


class ProgressReader:
    def __init__(self, handle, on_progress: Callable[[int], None]) -> None:
        self._handle = handle
        self._on_progress = on_progress
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._read += len(chunk)
            self._on_progress(self._read)
        return chunk

    def __getattr__(self, name):
        return getattr(self._handle, name)


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = {"files": {}}
        self.enabled = True
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as handle:
                    self.data = json.load(handle) or {"files": {}}
            if "files" not in self.data:
                self.data["files"] = {}
        except Exception as exc:
            log(f"state disabled: failed to load {self.path}: {exc}")
            self.enabled = False

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=True, indent=2)
        except Exception as exc:
            log(f"state disabled: failed to save {self.path}: {exc}")
            self.enabled = False

    def get(self, rel: str) -> dict:
        return self.data["files"].get(rel, {})

    def set(self, rel: str, payload: dict) -> None:
        self.data["files"][rel] = payload

    def delete(self, rel: str) -> None:
        self.data["files"].pop(rel, None)


class PermanentUploadError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"upload rejected ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class Syncer:
    def __init__(
        self,
        base_url,
        api_key,
        knowledge_id,
        watch_dir="/inbox",
        state_path="/tmp/openwebui-watcher-state.json",
        stable_age_seconds=10,
        progress_bytes=25 * 1024 * 1024,
        request_timeout=60,
        upload_timeout=300,
        retries=3,
        backoff_seconds=1.0,
        failed_dir_name="_upload_failed",
        status_every=10,
    ):
        # Ensure scheme for requests (e.g., http://openwebui:8080)
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        self.base_url = base_url.rstrip("/")
        self.knowledge_id = knowledge_id
        self.watch_dir = watch_dir
        self.stable_age_seconds = stable_age_seconds
        self.progress_bytes = progress_bytes
        self.request_timeout = request_timeout
        self.upload_timeout = upload_timeout
        self.retries = max(1, retries)
        self.backoff_seconds = max(0.1, backoff_seconds)
        self.status_every = max(1, status_every)
        failed_dir_name = (failed_dir_name or "_upload_failed").strip().strip("/\\")
        failed_dir_name = os.path.basename(failed_dir_name) or "_upload_failed"
        if failed_dir_name in {".", ".."}:
            failed_dir_name = "_upload_failed"
        self.failed_dir_name = failed_dir_name
        self.failed_dir = os.path.join(self.watch_dir, self.failed_dir_name)
        self.failed_meta_suffix = ".openwebui-error.json"
        self._failed_dir_warned = False
        self._stop_requested = False
        self.state = StateStore(state_path)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "openwebui-watcher/1.0",
            }
        )
        self._ensure_failed_dir()

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self.backoff_seconds * (2 ** attempt)
        time.sleep(delay)

    def _request_stop(self, reason: str) -> None:
        if not self._stop_requested:
            self._stop_requested = True
            log(f"stop requested: {reason}")

    def _progress_eta(self, done: int, total: int, started_at: float) -> str:
        if total <= 0:
            return ""
        elapsed = max(0.1, time.time() - started_at)
        rate = done / elapsed
        remaining = max(0, total - done)
        eta = int(remaining / rate) if rate > 0 else 0
        return f"{done}/{total} ({rate:.2f}/s, ETA {eta}s)"

    def _request(self, method: str, url: str, timeout: int, **kwargs) -> requests.Response:
        retry_statuses = {429, 500, 502, 503, 504}
        last_exc = None
        for attempt in range(self.retries):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
                if resp.status_code in retry_statuses and attempt < self.retries - 1:
                    self._sleep_backoff(attempt)
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retries - 1:
                    self._sleep_backoff(attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("request failed without exception")

    def _ensure_failed_dir(self) -> bool:
        try:
            os.makedirs(self.failed_dir, exist_ok=True)
            self._failed_dir_warned = False
            return True
        except OSError as exc:
            if not self._failed_dir_warned:
                log(f"quarantine disabled: cannot create {self.failed_dir}: {exc}")
                self._failed_dir_warned = True
            return False

    def _response_error_detail(self, resp: requests.Response | None) -> str:
        if resp is None:
            return "no response body"
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or payload.get("error")
                if detail:
                    return str(detail)
        except ValueError:
            pass
        text = (resp.text or "").strip().replace("\n", " ")
        if not text:
            return f"HTTP {resp.status_code}"
        if len(text) > 400:
            return f"{text[:397]}..."
        return text

    def _is_permanent_upload_status(self, status_code: int) -> bool:
        return status_code in {400, 413, 415, 422}

    def _looks_like_format_error(self, message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        keywords = (
            "unsupported",
            "invalid",
            "format",
            "mime",
            "extension",
            "parse",
            "parser",
            "decode",
            "unstructured",
            "file type",
        )
        return any(token in lowered for token in keywords)

    def _mark_blocked_file(self, rel: str, meta: dict, reason: str) -> None:
        if not self.state.enabled:
            return
        state = self.state.get(rel)
        state["blocked_reason"] = reason[:500]
        state["blocked_size"] = meta["size"]
        state["blocked_mtime"] = meta["mtime"]
        state["file_id"] = None
        state["added"] = False
        state.pop("replace_id", None)
        self.state.set(rel, state)

    def get_quarantined_names(self) -> set[str]:
        names: set[str] = set()
        if not os.path.isdir(self.failed_dir):
            return names
        try:
            for root, _, filenames in os.walk(self.failed_dir):
                for name in filenames:
                    if name.endswith(self.failed_meta_suffix):
                        continue
                    full = os.path.join(root, name)
                    if os.path.isfile(full):
                        names.add(name)
        except OSError as exc:
            log(f"sync: failed to scan quarantine folder: {exc}")
        return names

    def quarantine_file(self, rel: str, meta: dict, reason: str) -> bool:
        src = meta.get("full")
        if not src:
            self._mark_blocked_file(rel, meta, reason)
            return False
        if os.path.abspath(src).startswith(os.path.abspath(self.failed_dir)):
            return True
        if not self._ensure_failed_dir():
            self._mark_blocked_file(rel, meta, reason)
            return False
        dest = os.path.join(self.failed_dir, rel)
        dest_dir = os.path.dirname(dest)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            base, ext = os.path.splitext(dest)
            candidate = dest
            suffix = 1
            while os.path.exists(candidate):
                candidate = f"{base}.failed{suffix}{ext}"
                suffix += 1
            shutil.move(src, candidate)
            meta_payload = {
                "reason": reason,
                "original_relative_path": rel,
                "quarantined_at": datetime.now().isoformat(timespec="seconds"),
            }
            with open(f"{candidate}{self.failed_meta_suffix}", "w", encoding="utf-8") as handle:
                json.dump(meta_payload, handle, ensure_ascii=False, indent=2)
            if self.state.enabled:
                self.state.delete(rel)
            log(f"sync: quarantined {meta['name']} -> {os.path.relpath(candidate, self.watch_dir)}")
            return True
        except FileNotFoundError:
            log(f"sync: quarantine skipped (file disappeared): {meta.get('name', rel)}")
            if self.state.enabled:
                self.state.delete(rel)
            return False
        except OSError as exc:
            log(f"sync: quarantine failed for {meta.get('name', rel)}: {exc}")
            self._mark_blocked_file(rel, meta, reason)
            return False

    # ----- local -----
    def get_local_files(self) -> tuple[dict[str, dict], int]:
        files = {}
        ignored = 0
        ignored_dirs = {self.failed_dir_name.lower(), "ignore"}
        for root, dirnames, filenames in os.walk(self.watch_dir):
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() not in ignored_dirs and not d.startswith(".")
            ]
            for name in filenames:
                if (
                    name.startswith(".")
                    or name.endswith((".swp", ".tmp", "~"))
                    or name.endswith(self.failed_meta_suffix)
                ):
                    ignored += 1
                    continue
                full = os.path.join(root, name)
                try:
                    stat = os.stat(full)
                except FileNotFoundError:
                    ignored += 1
                    continue
                except OSError:
                    ignored += 1
                    continue
                if stat.st_size == 0:
                    ignored += 1
                    continue
                rel = os.path.relpath(full, self.watch_dir)
                files[rel] = {
                    "full": full,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "name": os.path.basename(rel),
                }
        return files, ignored

    # ----- remote -----
    def get_remote_items(self) -> list[dict]:
        base_url = f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/files"
        out: list[dict] = []
        page = 1
        total: int | None = None
        last_signature = None

        while True:
            resp = self._request(
                "GET",
                base_url,
                params={"page": page},
                timeout=self.request_timeout,
            )
            resp.raise_for_status()
            payload = resp.json()

            if isinstance(payload, dict):
                items = payload.get("items") or payload.get("files") or payload.get("data") or []
                raw_total = payload.get("total")
                if total is None and raw_total is not None:
                    try:
                        total = int(raw_total)
                    except (TypeError, ValueError):
                        total = None
            else:
                items = payload

            if not isinstance(items, list):
                break
            if not items:
                break

            signature = (
                len(items),
                (items[0].get("id") if isinstance(items[0], dict) else None),
                (items[-1].get("id") if isinstance(items[-1], dict) else None),
            )
            if signature == last_signature:
                break
            last_signature = signature

            out.extend(items)
            if total is not None and len(out) >= total:
                break
            page += 1

            if page > 1000:
                break

        return out

    def get_remote_files(self) -> dict[str, str]:
        files = self.get_remote_items()
        out = {}
        for item in files:
            meta = item.get("meta") or {}
            name = meta.get("name") or item.get("filename")
            file_id = item.get("id")
            if name and file_id:
                out[name] = file_id
        return out

    def upload_file(self, fullpath: str, display_name: str, on_progress: Callable[[int], None]) -> str:
        for attempt in range(self.retries):
            try:
                with open(fullpath, "rb") as handle:
                    wrapped = ProgressReader(handle, on_progress)
                    resp = self._request(
                        "POST",
                        f"{self.base_url}/api/v1/files/",
                        files={"file": (display_name, wrapped)},
                        timeout=self.upload_timeout,
                    )
                resp.raise_for_status()
                file_id = resp.json().get("id")
                if not file_id:
                    detail = self._response_error_detail(resp)
                    raise RuntimeError(f"no FILE_ID returned for {display_name}: {detail}")
                return file_id
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                detail = self._response_error_detail(exc.response)
                if self._is_permanent_upload_status(status_code):
                    raise PermanentUploadError(status_code, detail) from exc
                if attempt < self.retries - 1:
                    self._sleep_backoff(attempt)
                    continue
                raise
            except requests.RequestException:
                if attempt < self.retries - 1:
                    self._sleep_backoff(attempt)
                    continue
                raise
        raise RuntimeError(f"upload failed for {display_name}")

    def add_to_knowledge(self, file_id: str, name: str) -> tuple[bool, str | None]:
        url = f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/file/add"
        payloads = [
            {"file_id": file_id},
            {"file_ids": [file_id]},
            {"file_id": file_id, "metadata": {}},
            {"file_ids": [file_id], "metadatas": [{}]},
            {"file_id": file_id, "metadatas": [], "metadata": {}},
        ]
        last_error = None
        for payload in payloads:
            resp = self._request("POST", url, json=payload, timeout=self.request_timeout)
            if resp.status_code in (200, 201, 204):
                return True, None
            if resp.status_code in (400, 404, 422):
                last_error = f"{resp.status_code} {self._response_error_detail(resp)}"
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                last_error = str(exc)
                continue
        log(f"add failed for {name}: {last_error}")
        return False, last_error

    def delete_remote(self, file_id: str) -> None:
        resp = self._request(
            "POST",
            f"{self.base_url}/api/v1/knowledge/{self.knowledge_id}/file/remove",
            json={"file_id": file_id},
            timeout=self.request_timeout,
        )
        resp.raise_for_status()

    # ----- sync -----
    def sync_once(self) -> None:
        local, ignored = self.get_local_files()
        try:
            remote_items = self.get_remote_items()
        except requests.RequestException as exc:
            log(f"sync: remote list failed: {exc}")
            return
        remote_entries = []
        remote = {}
        for item in remote_items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") or {}
            name = meta.get("name") or item.get("filename")
            file_id = item.get("id")
            if name and file_id:
                remote_entries.append((name, file_id))
                remote[name] = file_id

        now = time.time()
        name_counts = {}
        for meta in local.values():
            name_counts[meta["name"]] = name_counts.get(meta["name"], 0) + 1
        duplicate_names = {name for name, count in name_counts.items() if count > 1}
        quarantined_names = self.get_quarantined_names()
        local_names = set(name_counts.keys()) | quarantined_names
        stable = {}
        if self.state.enabled:
            for rel in list(self.state.data.get("files", {}).keys()):
                if rel not in local:
                    self.state.delete(rel)
        for rel, meta in local.items():
            prev = self.state.get(rel)
            same = prev and prev.get("size") == meta["size"] and prev.get("mtime") == meta["mtime"]
            age = now - meta["mtime"]
            if (self.state.enabled and same and age >= self.stable_age_seconds) or (
                not self.state.enabled and age >= self.stable_age_seconds
            ):
                stable[rel] = meta
            blocked_reason = prev.get("blocked_reason") if prev else None
            blocked_size = prev.get("blocked_size") if prev else None
            blocked_mtime = prev.get("blocked_mtime") if prev else None
            if blocked_reason and (blocked_size != meta["size"] or blocked_mtime != meta["mtime"]):
                blocked_reason = None
                blocked_size = None
                blocked_mtime = None
            state = {
                "size": meta["size"],
                "mtime": meta["mtime"],
                "name": meta["name"],
                "file_id": prev.get("file_id") if prev else None,
                "added": prev.get("added", False) if prev else False,
                "uploaded_size": prev.get("uploaded_size") if prev else None,
                "uploaded_mtime": prev.get("uploaded_mtime") if prev else None,
                "blocked_reason": blocked_reason,
                "blocked_size": blocked_size,
                "blocked_mtime": blocked_mtime,
            }
            if self.state.enabled:
                self.state.set(rel, state)

        if not local_names and remote_entries:
            log("sync: local inbox empty; deleting all remote files")
            to_delete = list(remote_entries)
        else:
            to_delete = [(name, file_id) for name, file_id in remote_entries if name not in local_names]
        to_upload = []
        to_add = []
        blocked_skipped = 0
        for rel, meta in stable.items():
            if meta["name"] in duplicate_names:
                log(f"sync: duplicate name skipped: {meta['name']} (use unique filenames)")
                continue
            state = self.state.get(rel)
            if state.get("blocked_reason"):
                blocked_skipped += 1
                continue
            name = meta["name"]
            uploaded_matches = (
                state.get("uploaded_size") == meta["size"]
                and state.get("uploaded_mtime") == meta["mtime"]
                and state.get("added", False)
            )
            if name in remote and uploaded_matches:
                state["file_id"] = remote[name]
                state["added"] = True
                if self.state.enabled:
                    self.state.set(rel, state)
                continue
            if name in remote and not uploaded_matches:
                state["replace_id"] = remote[name]
                if self.state.enabled:
                    self.state.set(rel, state)
            if state.get("file_id") and not state.get("added", False):
                to_add.append((rel, meta, state["file_id"]))
                continue
            to_upload.append((rel, meta))

        total_bytes = sum(meta["size"] for _, meta in to_upload)

        if not to_upload and not to_delete:
            extra = []
            if quarantined_names:
                extra.append(f"quarantined {len(quarantined_names)}")
            if blocked_skipped:
                extra.append(f"blocked {blocked_skipped}")
            extra_text = f", {', '.join(extra)}" if extra else ""
            if ignored:
                log(
                    f"sync: no changes (local {len(local_names)}, remote {len(remote_entries)}, ignored {ignored}{extra_text})"
                )
            else:
                log(f"sync: no changes (local {len(local_names)}, remote {len(remote_entries)}{extra_text})")
            if self.state.enabled:
                self.state.save()
            return
        if to_upload or to_add or to_delete:
            log(
                f"sync: start (local {len(local_names)}, remote {len(remote_entries)}, "
                f"stable {len(stable)}, upload {len(to_upload)}, add {len(to_add)}, delete {len(to_delete)})"
            )
        if to_upload:
            log(f"sync: queued {len(to_upload)} upload(s), {format_bytes(total_bytes)} total")
        add_failed = 0
        upload_failed = 0
        uploaded_ok = 0
        quarantined = 0
        uploaded_bytes = 0
        upload_started = time.time()
        processed_uploads = 0
        for rel, meta in to_upload:
            if self._stop_requested:
                log("sync: stop requested, aborting upload loop")
                break
            last_logged = 0
            last_seen = 0

            def on_progress(read_bytes: int) -> None:
                nonlocal last_logged, last_seen, uploaded_bytes
                delta = read_bytes - last_seen
                last_seen = read_bytes
                if delta > 0:
                    uploaded_bytes += delta
                if read_bytes - last_logged < self.progress_bytes and read_bytes != meta["size"]:
                    return
                last_logged = read_bytes
                elapsed = max(0.1, time.time() - upload_started)
                rate = uploaded_bytes / elapsed
                remaining = max(0, total_bytes - uploaded_bytes)
                eta = remaining / rate if rate > 0 else 0
                log(
                    f"uploading {meta['name']}: {format_bytes(read_bytes)}/{format_bytes(meta['size'])} "
                    f"({format_bytes(uploaded_bytes)}/{format_bytes(total_bytes)} total, "
                    f"{format_bytes(int(rate))}/s, ETA {int(eta)}s)"
                )

            try:
                file_id = self.upload_file(meta["full"], meta["name"], on_progress)
            except PermanentUploadError as exc:
                if self.quarantine_file(rel, meta, str(exc)):
                    quarantined += 1
                else:
                    upload_failed += 1
                processed_uploads += 1
                continue
            except (requests.RequestException, OSError, RuntimeError) as exc:
                log(f"sync: upload failed for {meta['name']}: {exc}")
                upload_failed += 1
                processed_uploads += 1
                continue

            uploaded_ok += 1
            processed_uploads += 1
            state = self.state.get(rel)
            state["file_id"] = file_id
            state["added"] = False
            state["blocked_reason"] = None
            state["blocked_size"] = None
            state["blocked_mtime"] = None
            if self.state.enabled:
                self.state.set(rel, state)
            try:
                added, add_error = self.add_to_knowledge(file_id, meta["name"])
            except requests.RequestException as exc:
                log(f"sync: add failed for {meta['name']}: {exc}")
                add_failed += 1
                if processed_uploads % self.status_every == 0 or processed_uploads == len(to_upload):
                    log(
                        f"sync: upload progress {self._progress_eta(processed_uploads, len(to_upload), upload_started)} "
                        f"(ok {uploaded_ok}, failed {upload_failed}, quarantined {quarantined})"
                    )
                continue
            if not added:
                add_failed += 1
                if self._looks_like_format_error(add_error):
                    if self.quarantine_file(
                        rel,
                        meta,
                        f"knowledge add rejected: {add_error or 'unknown format error'}",
                    ):
                        quarantined += 1
            else:
                state["added"] = True
                state["uploaded_size"] = meta["size"]
                state["uploaded_mtime"] = meta["mtime"]
                if self.state.enabled:
                    self.state.set(rel, state)
                    if state.get("replace_id"):
                        try:
                            self.delete_remote(state["replace_id"])
                        except requests.RequestException as exc:
                            log(f"sync: replace delete failed for {meta['name']}: {exc}")
                        else:
                            state.pop("replace_id", None)
                            self.state.set(rel, state)
            if processed_uploads % self.status_every == 0 or processed_uploads == len(to_upload):
                log(
                    f"sync: upload progress {self._progress_eta(processed_uploads, len(to_upload), upload_started)} "
                    f"(ok {uploaded_ok}, failed {upload_failed}, quarantined {quarantined})"
                )

        add_started = time.time()
        processed_adds = 0
        for rel, meta, file_id in to_add:
            if self._stop_requested:
                log("sync: stop requested, aborting add loop")
                break
            try:
                added, add_error = self.add_to_knowledge(file_id, meta["name"])
            except requests.RequestException as exc:
                log(f"sync: add failed for {meta['name']}: {exc}")
                add_failed += 1
                processed_adds += 1
                if processed_adds % self.status_every == 0 or processed_adds == len(to_add):
                    log(
                        f"sync: add progress {self._progress_eta(processed_adds, len(to_add), add_started)} "
                        f"(failed {add_failed})"
                    )
                continue
            if added:
                state = self.state.get(rel)
                state["added"] = True
                state["uploaded_size"] = meta["size"]
                state["uploaded_mtime"] = meta["mtime"]
                state["blocked_reason"] = None
                state["blocked_size"] = None
                state["blocked_mtime"] = None
                if self.state.enabled:
                    self.state.set(rel, state)
                    if state.get("replace_id"):
                        try:
                            self.delete_remote(state["replace_id"])
                        except requests.RequestException as exc:
                            log(f"sync: replace delete failed for {meta['name']}: {exc}")
                        else:
                            state.pop("replace_id", None)
                            self.state.set(rel, state)
            else:
                add_failed += 1
                if self._looks_like_format_error(add_error):
                    if self.quarantine_file(
                        rel,
                        meta,
                        f"knowledge add rejected: {add_error or 'unknown format error'}",
                    ):
                        quarantined += 1
            processed_adds += 1
            if processed_adds % self.status_every == 0 or processed_adds == len(to_add):
                log(
                    f"sync: add progress {self._progress_eta(processed_adds, len(to_add), add_started)} "
                    f"(failed {add_failed})"
                )

        delete_started = time.time()
        deleted_ok = 0
        delete_failed = 0
        processed_deletes = 0
        for name, file_id in to_delete:
            if self._stop_requested:
                log("sync: stop requested, aborting delete loop")
                break
            try:
                self.delete_remote(file_id)
                deleted_ok += 1
            except requests.RequestException as exc:
                log(f"sync: delete failed for {name}: {exc}")
                delete_failed += 1
            processed_deletes += 1
            if processed_deletes % self.status_every == 0 or processed_deletes == len(to_delete):
                log(
                    f"sync: delete progress {self._progress_eta(processed_deletes, len(to_delete), delete_started)} "
                    f"(ok {deleted_ok}, failed {delete_failed})"
                )

        extra = []
        if quarantined:
            extra.append(f"quarantined {quarantined}")
        if blocked_skipped:
            extra.append(f"blocked {blocked_skipped}")
        if quarantined_names:
            extra.append(f"quarantine_pool {len(quarantined_names)}")
        if ignored:
            extra.append(f"ignored {ignored}")
        extra_text = f", {', '.join(extra)}" if extra else ""
        if ignored:
            log(
                f"sync: queued {len(to_upload)}, uploaded {uploaded_ok}, upload_failed {upload_failed}, "
                f"add_failed {add_failed}, delete {len(to_delete)} "
                f"(local {len(local_names)}, remote {len(remote_entries)}{extra_text})"
            )
        else:
            log(
                f"sync: queued {len(to_upload)}, uploaded {uploaded_ok}, upload_failed {upload_failed}, "
                f"add_failed {add_failed}, delete {len(to_delete)} "
                f"(local {len(local_names)}, remote {len(remote_entries)}{extra_text})"
            )
        if self.state.enabled:
            self.state.save()

    def run(self, interval_seconds: int = 5) -> None:
        log(f"start polling every {interval_seconds}s")
        def _handler(signum, _frame):
            try:
                name = signal.Signals(signum).name
            except Exception:
                name = str(signum)
            self._request_stop(name)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                continue
        try:
            self.sync_once()
            while True:
                if self._stop_requested:
                    break
                time.sleep(interval_seconds)
                try:
                    self.sync_once()
                except Exception as exc:
                    log(f"sync: error: {exc}")
        except KeyboardInterrupt:
            self._request_stop("KeyboardInterrupt")
        if self.state.enabled:
            self.state.save()
        log("stopped")


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} not set")
    # Allow .env values wrapped in quotes
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value


def env_optional(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = env_optional(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        log(f"{name} invalid ({raw}), using default {default}")
        return default
    if minimum is not None and value < minimum:
        log(f"{name} below minimum ({value} < {minimum}), using {minimum}")
        return minimum
    return value


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = env_optional(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        log(f"{name} invalid ({raw}), using default {default}")
        return default
    if minimum is not None and value < minimum:
        log(f"{name} below minimum ({value} < {minimum}), using {minimum}")
        return minimum
    return value


if __name__ == "__main__":
    base_url = env_required("BASE_URL")
    api_key = env_required("API_KEY")
    knowledge_id = env_required("KNOWLEDGE_ID")
    watch_dir = env_optional("WATCH_DIR", "/inbox")
    interval = env_int("SYNC_INTERVAL", 10, minimum=1)
    stable_age = env_int("STABLE_AGE_SECONDS", 10, minimum=0)
    state_path = env_optional("STATE_PATH", "/tmp/openwebui-watcher-state.json")
    progress_bytes = env_int("PROGRESS_BYTES", 25 * 1024 * 1024, minimum=1)
    request_timeout = env_int("REQUEST_TIMEOUT", 60, minimum=1)
    upload_timeout = env_int("UPLOAD_TIMEOUT", 300, minimum=1)
    retries = env_int("REQUEST_RETRIES", 3, minimum=1)
    backoff_seconds = env_float("REQUEST_BACKOFF_SECONDS", 1.0, minimum=0.1)
    failed_dir_name = env_optional("FAILED_DIR_NAME", "_upload_failed")
    status_every = env_int("STATUS_EVERY", 10, minimum=1)

    log("starting watcher")
    Syncer(
        base_url,
        api_key,
        knowledge_id,
        watch_dir,
        state_path=state_path,
        stable_age_seconds=stable_age,
        progress_bytes=progress_bytes,
        request_timeout=request_timeout,
        upload_timeout=upload_timeout,
        retries=retries,
        backoff_seconds=backoff_seconds,
        failed_dir_name=failed_dir_name,
        status_every=status_every,
    ).run(interval)
