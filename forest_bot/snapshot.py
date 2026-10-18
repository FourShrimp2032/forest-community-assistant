"""Bundled bootstrap snapshot of the original «Лісовий архів».

The snapshot (`snapshot/manifest.json` + `snapshot/files/`) contains only the original Drive archive: the
file bytes, Drive provenance (file ID, name, MIME type, modifiedTime, md5Checksum, link) and the extracted /
OCR text. No Telegram uploads, users, conversations, credentials or runtime database.

- `bootstrap()` imports it into an EMPTY database for the configured original archive folder only, reusing
  the bundled text (no OCR) and the original modifiedTime as version time. Drive sync then runs as usual:
  unchanged files match modifiedTime + md5 and are not downloaded again.
- `build_snapshot()` rebuilds it reproducibly from the Drive API listing. File bytes are reused from the
  existing snapshot or from seed directories only when their md5 matches Drive's md5Checksum; anything
  else is downloaded. Text is re-extracted only for changed bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .extraction import ExtractedText, ExtractionError, OcrSettings, extension_of, extract_text
from .storage import Store, from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
META_KEY = "bootstrap_snapshot"


class SnapshotError(Exception):
    pass


def load_manifest(snapshot_dir: Path) -> dict | None:
    path = Path(snapshot_dir) / "manifest.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text("utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise SnapshotError(f"unsupported snapshot format {manifest.get('format_version')!r}")
    return manifest


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_date(manifest_info: dict | None) -> str | None:
    """«18.10.2026» from stored bootstrap info."""
    if not manifest_info or not manifest_info.get("created_at"):
        return None
    return from_iso(manifest_info["created_at"]).strftime("%d.%m.%Y")


def bootstrap_info(store: Store) -> dict | None:
    raw = store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


@dataclass
class BootstrapResult:
    imported: int = 0
    skipped: str = ""          # reason when nothing was imported


async def bootstrap(store: Store, library, snapshot_dir: Path, folder_id: str) -> BootstrapResult:
    """Import the bundled snapshot into an empty database. Idempotent and conservative:

    - only for the configured folder the snapshot was taken from (never seeds test/custom folders);
    - only if the database has no documents and no Drive records and no bootstrap was recorded
      (existing installations keep their state; restarts never re-import or restore anything);
    - files whose bundled bytes fail the SHA-256 check are skipped and left for Drive sync.
    """
    if bootstrap_info(store):
        return BootstrapResult(skipped="already bootstrapped")
    try:
        manifest = load_manifest(snapshot_dir)
    except (SnapshotError, ValueError) as exc:
        log.error("Bootstrap snapshot unusable: %s", exc)
        return BootstrapResult(skipped=f"snapshot unusable: {exc}")
    if manifest is None:
        return BootstrapResult(skipped="no snapshot")
    if manifest.get("folder_id") != folder_id:
        log.info("Bootstrap snapshot is for folder %s, configured folder is %s: not imported",
                 manifest.get("folder_id"), folder_id)
        return BootstrapResult(skipped="different folder")
    if store.count_documents() or store.drive_files():
        return BootstrapResult(skipped="database not empty")

    files = sorted(manifest["files"], key=lambda e: (e["modified_time"], e["name"]))
    imported = 0
    for entry in files:
        path = Path(snapshot_dir) / entry["file"] if entry.get("file") else None
        data = path.read_bytes() if path and path.exists() else None
        if data is None or sha256(data) != entry.get("sha256"):
            log.warning("Snapshot file for «%s» missing or checksum mismatch: left for Drive sync", entry["name"])
            continue
        extracted = None
        error = entry.get("extraction_error")
        if not error:
            extracted = ExtractedText(entry["text"], entry["extraction_method"], list(entry.get("notes") or []))
        outcome = await library.ingest_drive_file(
            file_id=entry["id"], name=entry["name"], modified_time=entry["modified_time"], link=entry["web_link"],
            data=None if error else data, error=error, extracted=extracted)
        # The same row a completed sync of this file version would leave behind.
        store.upsert_drive_file(
            entry["id"], name=entry["name"], mime_type=entry["mime_type"], modified_time=entry["modified_time"],
            md5=entry.get("md5"), size=entry.get("size"), web_link=entry["web_link"], state="present",
            last_seen_at=manifest["created_at"], last_processed_modified=entry["modified_time"],
            last_document_id=outcome.doc.id, last_error=None)
        store.add_history(outcome.doc.id, "bootstrap_snapshot", snapshot_created_at=manifest["created_at"])
        imported += 1
    store.set_meta(META_KEY, json.dumps({"created_at": manifest["created_at"], "folder_id": folder_id,
                                         "imported": imported, "imported_at": to_iso(utcnow())}))
    log.info("Bootstrap snapshot (%s, %d files) imported: %d documents", manifest["created_at"],
             len(files), imported)
    return BootstrapResult(imported=imported)


# ---------------------------------------------------------------------------- building

def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _seed_index(seed_dirs: list[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for d in seed_dirs:
        for p in Path(d).iterdir():
            if p.is_file():
                index.setdefault(_nfc(p.name), p)
    return index


async def build_snapshot(client, folder_id: str, out_dir: Path, seed_dirs: list[Path] | None = None,
                         ocr: OcrSettings | None = None, download_delay: float = 2.0) -> dict:
    """(Re)build the snapshot in `out_dir` from a complete Drive listing of `folder_id`.

    Bytes are reused only when their md5 equals Drive's md5Checksum (existing snapshot first, then seed
    directories); otherwise they are downloaded through the Drive API. Raises on listing failure or a
    Google download block; already fetched files stay in `out_dir/files` for the next run."""
    out_dir = Path(out_dir)
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    previous = {}
    try:
        old = load_manifest(out_dir)
        previous = {e["id"]: e for e in (old or {}).get("files", [])}
    except SnapshotError:
        pass
    seeds = _seed_index(list(seed_dirs or []))
    listing = await client.list_folder(folder_id)
    entries, stats = [], {"reused": 0, "seeded": 0, "downloaded": 0, "extracted": 0}
    for f in sorted(listing, key=lambda x: x.name):
        rel = f"files/{f.id}{extension_of(f.effective_name) or '.bin'}"
        target = out_dir / rel
        data = None
        if target.exists() and f.md5 and _md5(target.read_bytes()) == f.md5:
            data = target.read_bytes()
            stats["reused"] += 1
        elif _nfc(f.name) in seeds and f.md5 and _md5(seeds[_nfc(f.name)].read_bytes()) == f.md5:
            data = seeds[_nfc(f.name)].read_bytes()
            target.write_bytes(data)
            stats["seeded"] += 1
        else:
            if download_delay:
                await asyncio.sleep(download_delay)
            data = await client.download(f)
            if f.md5 and _md5(data) != f.md5:
                raise SnapshotError(f"downloaded bytes of «{f.name}» do not match Drive md5Checksum")
            target.write_bytes(data)
            stats["downloaded"] += 1
        digest = sha256(data)
        entry = {"id": f.id, "name": _nfc(f.name), "mime_type": f.mime_type,
                 "modified_time": f.modified_time, "md5": f.md5, "size": f.size, "web_link": f.link,
                 "file": rel, "sha256": digest}
        prev = previous.get(f.id)
        if prev and prev.get("sha256") == digest and (prev.get("text") or prev.get("extraction_error")):
            for key in ("text", "extraction_method", "notes", "extraction_error"):
                if key in prev:
                    entry[key] = prev[key]
        else:
            try:
                result = await asyncio.to_thread(extract_text, data, f.effective_name, ocr or OcrSettings())
                entry.update(text=result.text, extraction_method=result.method, notes=result.notes)
            except ExtractionError as exc:
                entry.update(extraction_error=exc.reason)
            stats["extracted"] += 1
        entries.append(entry)
    manifest = {
        "format_version": FORMAT_VERSION,
        "description": "Bootstrap snapshot of the original «Лісовий архів» (Google Drive). Original files and "
                       "extracted text only; no user data.",
        "source": "Google Drive API v3",
        "folder_id": folder_id,
        "created_at": to_iso(datetime.now(timezone.utc)),
        "file_count": len(entries),
        "files": entries,
    }
    referenced = {e["file"] for e in entries}
    for p in files_dir.iterdir():
        if f"files/{p.name}" not in referenced:
            p.unlink()
    tmp = out_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    shutil.move(tmp, out_dir / "manifest.json")
    log.info("Snapshot built: %d files (%s)", len(entries), stats)
    manifest["_stats"] = stats
    return manifest
