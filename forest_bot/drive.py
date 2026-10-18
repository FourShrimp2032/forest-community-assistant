"""Google Drive archive access (Drive API v3 with an API key) and synchronisation.

Safety rules:
- Only a complete, successful listing (all pages, all sub-folders) may establish that a file is gone.
- Any listing error aborts the sync without touching existing documents.
- Unchanged files (same modifiedTime and checksum) are skipped; unchanged content never creates a version.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .extraction import SUPPORTED_EXTENSIONS, extension_of
from .library import Library, Outcome
from .storage import Store, from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


class DriveError(Exception):
    pass


class DriveThrottled(DriveError):
    """Google's anti-automation block ("your computer or network may be sending automated queries")."""


class DriveRetryLater(DriveError):
    """Google asked for a Retry-After delay longer than we wait inline; try again after `retry_after` s."""

    def __init__(self, retry_after: float, message: str = ""):
        super().__init__(message or f"Google просить повторити через {retry_after:.0f} с")
        self.retry_after = retry_after


class DrivePermanentError(DriveError):
    """A file cannot be downloaded now; it is recorded as rejected and shown in /pending.

    kind="access":  permission/credential/not-found problems. Access can be restored without the file
                    changing, so the download is rechecked at bounded intervals.
    kind="content": the file itself is unusable (too large). Waits for a file change."""

    def __init__(self, message: str, kind: str = "access"):
        super().__init__(message)
        self.kind = kind


RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "downloadQuotaExceeded", "quotaExceeded",
                      "sharingRateLimitExceeded", "dailyLimitExceeded"}


def _error_reasons(resp: httpx.Response) -> set[str]:
    try:
        error = resp.json().get("error", {})
    except Exception:
        return set()
    reasons = {e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)}
    reasons |= {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
    return {r for r in reasons if r}


def classify_failure(resp: httpx.Response) -> str:
    """'block' | 'transient' | 'access' for a non-200 response that is not a plain 429/5xx retry.

    The response content decides first: Google's HTML anti-automation page and Drive's rate-limit reasons
    are temporary even though they arrive as HTTP 403. Only then is the status used."""
    content_type = resp.headers.get("content-type", "")
    text = resp.text[:4000].lower()
    # Only Google's anti-automation page counts as a block; other HTML error pages (e.g. a generic
    # "Sorry, the file … does not exist") fall through to the status-based rules.
    if "text/html" in content_type and ("automated queries" in text or "unusual traffic" in text):
        return "block"
    if _error_reasons(resp) & RATE_LIMIT_REASONS:
        return "transient"
    if resp.status_code in (400, 401, 403, 404, 410):
        return "access"
    return "transient"


MAX_INLINE_RETRY_AFTER = 30.0
DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str           # normalised UTC ISO
    md5: str | None = None
    size: int | None = None
    web_link: str | None = None

    @property
    def link(self) -> str:
        return self.web_link or f"https://drive.google.com/file/d/{self.id}/view"

    @property
    def effective_name(self) -> str:
        """Name used for extraction; Google Docs are exported as plain text."""
        if self.mime_type == GOOGLE_DOC_MIME and not self.name.lower().endswith(".txt"):
            return self.name + ".txt"
        return self.name


class DriveClient:
    def __init__(self, api_key: str, timeout: float = 60.0, recursive: bool = True,
                 max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES, page_size: int = 1000):
        self.api_key = api_key
        self.page_size = page_size
        self.recursive = recursive
        self.max_download_bytes = max_download_bytes
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict) -> httpx.Response:
        params = {**params, "key": self.api_key}
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = await self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = DriveError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                retry_after = resp.headers.get("retry-after", "")
                requested = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None
                if requested is not None and requested > MAX_INLINE_RETRY_AFTER:
                    # Never retry before the requested time; defer instead of blocking this sync.
                    raise DriveRetryLater(requested, f"HTTP {resp.status_code}, Retry-After {requested:.0f} s")
                if attempt < 3:
                    await asyncio.sleep(requested if requested is not None else 2 ** attempt)
                continue
            if resp.status_code != 200:
                kind = classify_failure(resp)
                if kind == "block":
                    raise DriveThrottled(f"Google тимчасово блокує автоматичні запити з цієї мережі "
                                         f"(HTTP {resp.status_code})")
                reasons = ", ".join(sorted(_error_reasons(resp))) or "без причини"
                if kind == "transient":
                    raise DriveThrottled(f"тимчасове обмеження Google (HTTP {resp.status_code}: {reasons})")
                raise DrivePermanentError(f"HTTP {resp.status_code}: {reasons}", kind="access")
            return resp
        raise DriveError(f"request failed after retries: {last_exc}")

    async def list_folder(self, folder_id: str) -> list[DriveFile]:
        """Complete listing of non-trashed files (recursively). Raises DriveError on any failure."""
        files: list[DriveFile] = []
        queue = [folder_id]
        seen = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            page_token = None
            while True:
                params = {
                    "q": f"'{current}' in parents and trashed = false",
                    "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum, size, webViewLink)",
                    "pageSize": self.page_size,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if page_token:
                    params["pageToken"] = page_token
                data = (await self._get(f"{API}/files", params)).json()
                if "files" not in data:
                    raise DriveError("unexpected listing response (no 'files')")
                for f in data["files"]:
                    if f.get("mimeType") == FOLDER_MIME:
                        if self.recursive:
                            queue.append(f["id"])
                        continue
                    if not f.get("modifiedTime"):
                        raise DriveError(f"file {f.get('id')} has no modifiedTime")
                    files.append(DriveFile(
                        id=f["id"], name=f.get("name", ""), mime_type=f.get("mimeType", ""),
                        modified_time=to_iso(from_iso(f["modifiedTime"])), md5=f.get("md5Checksum"),
                        size=int(f["size"]) if f.get("size") else None, web_link=f.get("webViewLink")))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return files

    async def download(self, f: DriveFile) -> bytes:
        if f.size and f.size > self.max_download_bytes:
            raise DrivePermanentError(too_large_reason(f.size, self.max_download_bytes), kind="content")
        if f.mime_type == GOOGLE_DOC_MIME:
            resp = await self._get(f"{API}/files/{f.id}/export", {"mimeType": "text/plain"})
        else:
            resp = await self._get(f"{API}/files/{f.id}", {"alt": "media", "supportsAllDrives": "true"})
        return resp.content


@dataclass
class SyncReport:
    ok: bool
    message: str = ""
    listed: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    pending: int = 0
    errors: int = 0
    outcomes: list[Outcome] = field(default_factory=list)


ACCESS_RECHECK_BASE_SECONDS = 3600
ACCESS_RECHECK_MAX_SECONDS = 24 * 3600


def access_recheck_delay(failures: int) -> int:
    """1 h, 2 h, 4 h, … up to 24 h after consecutive download-access failures of the same file."""
    return min(ACCESS_RECHECK_BASE_SECONDS * 2 ** max(failures - 1, 0), ACCESS_RECHECK_MAX_SECONDS)


def too_large_reason(size: int, limit: int) -> str:
    return f"файл завеликий ({size / 1024 / 1024:.1f} МБ; ліміт {limit // (1024 * 1024)} МБ)"


def throttle_retry_delay(streak: int, base: float, cap: float) -> float:
    """Bounded exponential backoff after Google's download block: base, 2×base, 4×base, … ≤ cap."""
    return min(base * (2 ** max(streak - 1, 0)), cap)


@dataclass
class ArchiveStatus:
    """Whether the initial archive is still incomplete (from stored state, not from a running sync).

    waiting: files in the last successful listing that were never processed (download deferred by a
    block, Retry-After or a transient error). Rejected files (in /pending) are not "waiting"; neither is a
    changed file whose previous version is still active."""
    enabled: bool
    listed_once: bool
    total: int
    waiting: int

    @property
    def loading(self) -> bool:
        return self.enabled and (not self.listed_once or self.waiting > 0)


def archive_status(store: Store, sync_enabled: bool) -> ArchiveStatus:
    if not sync_enabled:
        return ArchiveStatus(False, True, 0, 0)
    listed_once = store.has_successful_sync()
    present = store.drive_files("present")
    waiting = sum(1 for f in present if not f.get("last_processed_modified"))
    return ArchiveStatus(True, listed_once, len(present), waiting)


class DriveSync:
    def __init__(self, client, store: Store, library: Library, folder_id: str, max_delete_fraction: float = 0.5,
                 download_delay: float = 0.0):
        self.client = client
        self.download_delay = download_delay
        self.throttled = False      # last run was stopped by Google's anti-automation block
        self.throttle_streak = 0    # consecutive runs stopped by it (reset by a run without a block)
        self.retry_after = 0.0      # Google-requested delay (s) before the next attempt; 0 = none
        self.not_before = 0.0       # monotonic time before which no request is sent (from Retry-After)
        self.store = store
        self.library = library
        self.folder_id = folder_id
        self.max_delete_fraction = max_delete_fraction
        self._running = asyncio.Lock()

    async def run(self) -> SyncReport:
        if self._running.locked():
            log.info("Drive sync already running; skipped")
            return SyncReport(ok=False, message="already running")
        wait = self.not_before - time.monotonic()
        if wait > 0:
            log.info("Drive sync skipped: Google asked to wait %.0f s more", wait)
            return SyncReport(ok=False, message=f"відкладено на прохання Google ще на {wait:.0f} с")
        async with self._running:
            run_id = self.store.start_sync_run()
            started = datetime.now()
            try:
                report = await self._run()
            except Exception as exc:  # never let a sync failure touch documents
                log.exception("Drive sync failed: %s", exc)
                report = SyncReport(ok=False, message=f"помилка: {exc}")
            if self.retry_after:
                self.not_before = time.monotonic() + self.retry_after
            self.store.finish_sync_run(run_id, report.ok, report.message, listed=report.listed, added=report.added,
                                       updated=report.updated, unchanged=report.unchanged, removed=report.removed,
                                       pending=report.pending, errors=report.errors)
            log.info("Drive sync %s in %.1fs: %s (listed=%d added=%d updated=%d unchanged=%d removed=%d "
                     "pending=%d errors=%d)", "OK" if report.ok else "FAILED",
                     (datetime.now() - started).total_seconds(), report.message, report.listed, report.added,
                     report.updated, report.unchanged, report.removed, report.pending, report.errors)
            return report

    async def _run(self) -> SyncReport:
        self.retry_after = 0.0
        try:
            files = await self.client.list_folder(self.folder_id)
        except DriveRetryLater as exc:
            self.retry_after = exc.retry_after
            return SyncReport(ok=False, message=f"перелік відкладено: {exc}")
        except Exception as exc:
            # Not evidence of deletion: keep everything as it is.
            return SyncReport(ok=False, message=f"не вдалося отримати список файлів: {exc}")

        report = SyncReport(ok=True, listed=len(files))
        now = to_iso(utcnow())
        self.throttled = False
        throttled_at: str | None = None
        listed_ids = {f.id for f in files}

        # Oldest first, so that versions of the same logical document arrive in source order.
        for f in sorted(files, key=lambda x: (x.modified_time, x.name)):
            known = self.store.get_drive_file(f.id)
            self.store.upsert_drive_file(f.id, name=f.name, mime_type=f.mime_type, modified_time=f.modified_time,
                                         md5=f.md5, size=f.size, web_link=f.link, state="present", last_seen_at=now)
            access_recheck_due = bool(known and known.get("access_retry_at")
                                      and from_iso(known["access_retry_at"]) <= utcnow())
            if known and known["state"] == "present" and known.get("last_processed_modified") == f.modified_time \
                    and (known.get("md5") or None) == (f.md5 or None) and not known.get("last_error") \
                    and not access_recheck_due:
                report.unchanged += 1
                continue
            if known and known.get("last_processed_modified") and \
                    from_iso(f.modified_time) < from_iso(known["last_processed_modified"]):
                log.warning("Drive file %s reports an older modifiedTime than already processed; ignored", f.id)
                report.unchanged += 1
                continue

            is_new = known is None or known["state"] != "present" or not known.get("last_processed_modified")
            limit = getattr(self.client, "max_download_bytes", DEFAULT_MAX_DOWNLOAD_BYTES)
            if extension_of(f.effective_name) not in SUPPORTED_EXTENSIONS:
                outcome = await self.library.ingest_drive_file(
                    file_id=f.id, name=f.effective_name, modified_time=f.modified_time, link=f.link, data=None,
                    error=f"формат «{extension_of(f.name) or f.mime_type}» не підтримується (лише PDF, DOCX, TXT)")
            elif f.size and f.size > limit:
                # Permanent: rejected (shown in /pending), retried only when the file changes.
                outcome = await self.library.ingest_drive_file(
                    file_id=f.id, name=f.effective_name, modified_time=f.modified_time, link=f.link, data=None,
                    error=too_large_reason(f.size, limit))
            elif throttled_at is not None:
                report.errors += 1          # deferred: retried on the next sync
                continue
            else:
                if self.download_delay:
                    await asyncio.sleep(self.download_delay)
                try:
                    data = await self.client.download(f)
                except DriveRetryLater as exc:
                    log.warning("Google asked to retry «%s» after %.0f s; remaining downloads deferred",
                                f.name, exc.retry_after)
                    throttled_at = f.name
                    self.retry_after = max(self.retry_after, exc.retry_after)
                    report.errors += 1
                    continue
                except DriveThrottled as exc:
                    log.warning("Drive downloads blocked by Google at «%s»: %s. Remaining changed files are "
                                "deferred to the next sync; existing documents stay available.", f.name, exc)
                    throttled_at = f.name
                    self.throttled = True
                    report.errors += 1
                    continue
                except DrivePermanentError as exc:
                    failures = (known or {}).get("access_failures", 0) + 1 if exc.kind == "access" else 0
                    retry_at = (to_iso(utcnow() + timedelta(seconds=access_recheck_delay(failures)))
                                if exc.kind == "access" else None)
                    log.warning("Download of %s «%s» rejected (%s): %s%s", f.id, f.name, exc.kind, exc,
                                f"; access recheck at {retry_at}" if retry_at else "")
                    reason = (f"немає доступу до файлу ({str(exc)[:80]}); перевірка повториться автоматично"
                              if exc.kind == "access" else str(exc))
                    if access_recheck_due and known and known.get("last_document_id"):
                        # Still no access: keep the existing rejection record, just reschedule.
                        outcome = None
                    else:
                        outcome = await self.library.ingest_drive_file(
                            file_id=f.id, name=f.effective_name, modified_time=f.modified_time, link=f.link,
                            data=None, error=reason)
                    self.store.upsert_drive_file(
                        f.id, last_processed_modified=f.modified_time, last_error=None,
                        access_failures=failures, access_retry_at=retry_at,
                        **({"last_document_id": outcome.doc.id} if outcome else {}))
                    if outcome:
                        report.outcomes.append(outcome)
                    report.pending += 1
                    continue
                except Exception as exc:
                    # Transient download problem: keep the previous version, retry next sync.
                    log.warning("Download failed for %s «%s»: %s", f.id, f.name, exc)
                    self.store.upsert_drive_file(f.id, last_error=str(exc)[:500])
                    report.errors += 1
                    continue
                outcome = await self.library.ingest_drive_file(
                    file_id=f.id, name=f.effective_name, modified_time=f.modified_time, link=f.link, data=data)
            self.store.upsert_drive_file(f.id, last_processed_modified=f.modified_time,
                                         last_document_id=outcome.doc.id, last_error=None,
                                         access_failures=0, access_retry_at=None)
            report.outcomes.append(outcome)
            if outcome.kind in ("need_location", "failed"):
                report.pending += 1
            elif outcome.kind == "duplicate":
                report.unchanged += 1
            elif is_new:
                report.added += 1
            else:
                report.updated += 1

        # Deletions: only after the complete listing above succeeded.
        gone = [f for f in self.store.drive_files("present") if f["file_id"] not in listed_ids]
        known_present = len(self.store.drive_files("present"))
        if gone and known_present and len(gone) / known_present > self.max_delete_fraction and len(gone) > 3:
            # Unusually many files missing: confirm with an independent second complete listing before
            # removing anything. Files missing from BOTH listings are removed regardless of their number;
            # files that reappear were only missing from an incomplete first listing and are kept.
            log.warning("Listing lacks %d of %d known files (> DRIVE_MAX_DELETE_FRACTION=%s); confirming with a "
                        "second listing before removing them", len(gone), known_present, self.max_delete_fraction)
            try:
                confirm_ids = {f.id for f in await self.client.list_folder(self.folder_id)}
            except Exception as exc:
                report.message = (f"{len(gone)} відсутніх файлів не підтверджено (повторний перелік не вдався: "
                                  f"{exc}); видалення відкладено до наступної синхронізації")
                log.warning(report.message)
                return report
            reappeared = [f for f in gone if f["file_id"] in confirm_ids]
            gone = [f for f in gone if f["file_id"] not in confirm_ids]
            report.message = (f"масове зникнення підтверджено повторним переліком: видалено {len(gone)}"
                              + (f", {len(reappeared)} файлів знову в переліку — залишено" if reappeared else ""))
            log.warning(report.message)
        for f in gone:
            self.library.remove_drive_file(f["file_id"])
            self.store.upsert_drive_file(f["file_id"], state="deleted", last_seen_at=f.get("last_seen_at"))
            report.removed += 1
        self.throttle_streak = self.throttle_streak + 1 if throttled_at is not None else 0
        self.throttled = throttled_at is not None
        if throttled_at is not None:
            report.message = f"завантаження тимчасово заблоковані Google (з «{throttled_at}»); решту відкладено"
        report.message = report.message or "ok"
        return report
