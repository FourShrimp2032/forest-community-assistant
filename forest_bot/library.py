"""Document lifecycle: ingestion decisions, versioning, replacement, withdrawal and removal.

All status, ownership, timestamp and provenance rules are enforced here in application code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Awaitable, Callable

from . import geography as geo
from . import storage as st
from .extraction import ExtractionError, OcrSettings, extract_text, extension_of, first_heading
from .geography import nfc, normalize
from .scope import ScopeResult, build_chunks, detect_document_scope
from .storage import Document, Store, to_iso

log = logging.getLogger(__name__)

ScopeClassifier = Callable[[str, str], Awaitable[ScopeResult | None]]

UPDATE_STATEMENT = re.compile(
    r"(оновлен\w*\s+(верс\w*|редакц\w*|документ\w*)|нов\w+\s+(редакц\w*|верс\w*)|"
    r"\bзамінює\b|\bскасовує\b|\bоновлює\b|замість\s+(попередн|старо|чинно)\w*|"
    r"\bоновлено\b|актуальн\w+\s+верс\w*)"
)


def norm_title(title: str) -> str:
    t = nfc(title).lower()
    t = re.sub(r"[’ʼ`´‘]", "'", t)
    t = re.sub(r"\.(pdf|docx|txt)$", "", t)
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", t)              # "(1)", "(оновлено)", "[копія]"
    t = re.sub(r"\s*[-–—_]\s*(копія|copy|оновлено|нова редакція|v\d+)\s*$", "", t)
    t = re.sub(r"[«»\"“”„]", " ", t)
    t = re.sub(r"[^\w']+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def content_hash(text: str) -> str:
    canon = re.sub(r"\s+", " ", nfc(text)).strip().lower()
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Outcome:
    """Result of an ingestion step. `kind` drives the Telegram dialogue and Drive bookkeeping."""
    kind: str  # activated | replaced | older | duplicate | need_location | need_replacement | failed | kept_separate
    doc: Document
    other: Document | None = None
    message: str = ""
    notes: list[str] = field(default_factory=list)


class Library:
    def __init__(
        self,
        store: Store,
        files_dir: Path,
        ocr: OcrSettings | None = None,
        scope_classifier: ScopeClassifier | None = None,
        embedder=None,
        chunk_max_chars: int = 900,
    ):
        self.store = store
        self.files_dir = Path(files_dir)
        self.ocr = ocr or OcrSettings()
        self.scope_classifier = scope_classifier
        self.embedder = embedder
        self.chunk_max_chars = chunk_max_chars
        self.revision = 0              # bumped whenever the set of active chunks changes
        self.lock = asyncio.Lock()     # serialises lifecycle decisions (Drive sync vs Telegram)

    # ------------------------------------------------------------------ extraction
    async def extract(self, data: bytes, filename: str):
        return await asyncio.to_thread(extract_text, data, filename, self.ocr)

    def save_file(self, doc_id: int, data: bytes, filename: str) -> str:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        path = self.files_dir / f"{doc_id}{extension_of(filename) or '.bin'}"
        path.write_bytes(data)
        return str(path)

    @staticmethod
    def titles_for(source: str, filename: str, text: str) -> tuple[str, str | None]:
        """Display title and an alternative normalised title key.

        Drive: the file name is the canonical title (archive names equal document headings).
        Telegram: the document's own heading if it has one (file names are often arbitrary).
        """
        stem = nfc(Path(filename).stem).strip() or "Без назви"
        heading = first_heading(text)
        if source == "drive" or not heading:
            title, alt = stem, heading
        else:
            title, alt = heading, stem
        alt_norm = norm_title(alt) if alt else None
        if alt_norm == norm_title(title):
            alt_norm = None
        return title, alt_norm

    async def resolve_scope(self, title: str, text: str) -> ScopeResult | None:
        result = detect_document_scope(title, text)
        if result:
            return result
        if self.scope_classifier:
            try:
                result = await self.scope_classifier(title, text)
            except Exception as exc:  # LLM unavailable -> treat as unresolved, never guess
                log.warning("LLM scope classification failed for %r: %s", title, exc)
                result = None
        return result

    # ------------------------------------------------------------------ chunks / index
    async def _index_document(self, doc: Document) -> None:
        drafts = build_chunks(doc.title, doc.text or "", doc.scope or [], self.chunk_max_chars)
        rows = [{"text": d.text, "scope": d.scope, "note": d.note} for d in drafts]
        if self.embedder is not None and self.embedder.available:
            try:
                vectors = await asyncio.to_thread(self.embedder.embed_passages,
                                                  [f"{doc.title}\n{r['text']}" for r in rows])
                for r, v in zip(rows, vectors):
                    r["embedding"] = v
                    r["embedding_model"] = self.embedder.model_name
            except Exception as exc:
                log.warning("Embedding failed for document %s: %s (keyword search still works)", doc.id, exc)
        self.store.replace_chunks(doc.id, rows)

    async def reindex_missing_embeddings(self) -> int:
        """Embed active chunks that lack vectors for the current model (e.g. after a model change)."""
        if self.embedder is None or not self.embedder.available:
            return 0
        missing = [c for c in self.store.active_chunks() if c.embedding_model != self.embedder.model_name]
        if not missing:
            return 0
        titles = {d.id: d.title for d in self.store.active_documents()}
        vectors = await asyncio.to_thread(self.embedder.embed_passages,
                                          [f"{titles.get(c.document_id, '')}\n{c.text}" for c in missing])
        for c, v in zip(missing, vectors):
            self.store.set_chunk_embedding(c.id, v, self.embedder.model_name)
        self._changed()
        log.info("Computed embeddings for %d chunks", len(missing))
        return len(missing)

    def _changed(self) -> None:
        self.revision += 1

    # ------------------------------------------------------------------ identity
    def _active_others(self, doc: Document) -> list[Document]:
        return [d for d in self.store.active_documents()
                if d.id != doc.id and not (doc.drive_file_id and d.drive_file_id == doc.drive_file_id)]

    def identity_candidates(self, doc: Document) -> tuple[list[Document], list[tuple[Document, str]]]:
        """(strong replacement candidates, ambiguous candidates with a reason).

        Strong: same title (name) and same geographic scope, or an explicit update statement in the
        content that names the existing document's title, for the same scope.
        Ambiguous: same title but only partly overlapping scope, a very similar title for the same
        scope, or an update statement whose target is not named exactly.
        Same topic + same place alone is NOT an identity signal.
        """
        strong: list[Document] = []
        ambiguous: list[tuple[Document, str]] = []
        scope = set(doc.scope or [])
        text_norm = re.sub(r"\s+", " ", normalize(doc.text or ""))
        has_update = bool(UPDATE_STATEMENT.search(text_norm))
        for other in self._active_others(doc):
            other_scope = set(other.scope or [])
            same_scope = other_scope == scope
            title_match = bool(doc.title_keys & other.title_keys)
            similarity = max(title_similarity(a, b) for a in doc.title_keys for b in other.title_keys)
            names_other = norm_title(other.title) in norm_title(doc.text or "")
            if same_scope and title_match:
                strong.append(other)
            elif same_scope and has_update and names_other:
                strong.append(other)
            elif title_match and scope & other_scope:
                ambiguous.append((other, "та сама назва, але інше місце застосування"))
            elif same_scope and similarity >= 0.75:
                ambiguous.append((other, "дуже схожа назва для того самого місця"))
            elif same_scope and any(a and b and (a in b or b in a)
                                    for a in doc.title_keys for b in other.title_keys):
                ambiguous.append((other, "назва одного документа містить назву іншого, місце те саме"))
            elif same_scope and has_update and similarity >= 0.5:
                ambiguous.append((other, "документ каже, що він оновлює інший документ"))
        strong.sort(key=lambda d: d.version_dt, reverse=True)
        ambiguous.sort(key=lambda x: title_similarity(doc.norm_title, x[0].norm_title), reverse=True)
        return strong, ambiguous

    def _duplicate_of(self, doc: Document) -> Document | None:
        """Identical content already active, or an old version whose logical document has a newer one."""
        for other in self.store.find_by_hash(doc.content_hash, (st.ACTIVE,)):
            if other.id != doc.id:
                return other
        for other in self.store.find_by_hash(doc.content_hash, (st.SUPERSEDED,)):
            if other.id != doc.id and self._active_in_logical(other.logical_id):
                return other
        return None

    def _active_in_logical(self, logical_id: int | None) -> Document | None:
        if logical_id is None:
            return None
        docs = self.store.documents("logical_id = ? AND status = ?", (logical_id, st.ACTIVE))
        return docs[-1] if docs else None

    # ------------------------------------------------------------------ state transitions
    async def _activate(self, doc: Document, note: str) -> Document:
        await self._index_document(doc)
        doc = self.store.update_document(doc.id, status=st.ACTIVE, status_reason=None, candidate_id=None)
        self.store.add_history(doc.id, "activated", note=note)
        self._changed()
        log.info("Document %s «%s» activated (%s), scope=%s", doc.id, doc.title, note, doc.scope)
        return self.store.get_document(doc.id)

    async def _replace(self, new: Document, old: Document, signal: str) -> Outcome:
        """Replace `old` by `new` only if `new` is genuinely newer (source timestamps)."""
        if new.version_dt <= old.version_dt:
            new = self.store.update_document(
                new.id, status=st.SUPERSEDED, superseded_by=old.id, logical_id=old.logical_id,
                status_reason=f"старіша або така сама за часом, ніж чинна версія №{old.id}", candidate_id=None)
            self.store.add_history(new.id, "kept_older", signal=signal, active_id=old.id,
                                   new_ts=new.version_ts, active_ts=old.version_ts)
            log.info("Document %s not activated: older than active %s (%s <= %s)",
                     new.id, old.id, new.version_ts, old.version_ts)
            return Outcome("older", new, old)
        new = self.store.update_document(new.id, logical_id=old.logical_id)
        await self._index_document(new)
        with self.store.transaction():
            self.store.update_document(old.id, status=st.SUPERSEDED, superseded_by=new.id,
                                       status_reason=f"замінено новішою версією №{new.id}")
            self.store.update_document(new.id, status=st.ACTIVE, status_reason=None, candidate_id=None)
        self.store.add_history(old.id, "superseded", by=new.id, signal=signal)
        self.store.add_history(new.id, "replaced", previous=old.id, signal=signal,
                               new_ts=new.version_ts, previous_ts=old.version_ts)
        self._changed()
        log.info("Document %s «%s» replaced %s (%s)", new.id, new.title, old.id, signal)
        return Outcome("replaced", self.store.get_document(new.id), self.store.get_document(old.id))

    async def _decide_identity(self, doc: Document) -> Outcome:
        """After extraction and scope resolution: duplicate / replace / ask / activate."""
        dup = self._duplicate_of(doc)
        if dup is not None:
            doc = self.store.update_document(doc.id, status=st.DUPLICATE, logical_id=dup.logical_id,
                                             status_reason=f"такий самий вміст, як у документа №{dup.id}")
            self.store.add_history(doc.id, "duplicate", of=dup.id)
            return Outcome("duplicate", doc, dup)

        strong, ambiguous = self.identity_candidates(doc)
        if strong:
            return await self._replace(doc, strong[0], "збіг назви та місця" if doc.title_keys & strong[0].title_keys
                                       else "у документі сказано, що він оновлює цей документ")
        if ambiguous:
            candidate, reason = ambiguous[0]
            if doc.source == "telegram":
                doc = self.store.update_document(doc.id, status=st.PENDING_REPLACEMENT, candidate_id=candidate.id,
                                                 status_reason=reason)
                self.store.add_history(doc.id, "ask_replacement", candidate=candidate.id, reason=reason)
                return Outcome("need_replacement", doc, candidate, reason)
            # Drive: never supersede on ambiguous identity; keep both and let answers show conflicts.
            doc = await self._activate(doc, "окремий документ")
            self.store.add_history(doc.id, "possible_replacement_not_applied", candidate=candidate.id, reason=reason)
            return Outcome("kept_separate", doc, candidate, reason)
        doc = await self._activate(doc, "новий документ")
        return Outcome("activated", doc)

    async def _after_extraction(self, doc: Document) -> Outcome:
        scope = await self.resolve_scope(doc.title, doc.text or "")
        if scope is None:
            doc = self.store.update_document(doc.id, status=st.PENDING_LOCATION,
                                             status_reason="не вдалося визначити, до якого місця належить документ")
            self.store.add_history(doc.id, "pending_location")
            return Outcome("need_location", doc)
        doc = self.store.update_document(doc.id, scope=geo.normalize_scope(scope.codes), scope_method=scope.method)
        self.store.add_history(doc.id, "scope", codes=doc.scope, method=scope.method, evidence=scope.evidence[:200])
        return await self._decide_identity(doc)

    # ------------------------------------------------------------------ Telegram
    def create_telegram_upload(self, *, user_id: int, chat_id: int, tg_file_id: str, filename: str,
                               received_at: datetime, uploader_name: str | None) -> Document:
        doc = self.store.insert_document(
            source="telegram", title=nfc(Path(filename).stem) or "Без назви", norm_title=norm_title(Path(filename).stem),
            filename=nfc(filename), status=st.AWAITING_NAME if not uploader_name else st.PROCESSING,
            version_ts=to_iso(received_at), tg_file_id=tg_file_id, tg_chat_id=chat_id,
            uploader_id=user_id, uploader_name=uploader_name,
        )
        self.store.add_history(doc.id, "received", via="telegram", user_id=user_id)
        return doc

    def set_uploader_name(self, doc_id: int, name: str) -> Document:
        return self.store.update_document(doc_id, uploader_name=name, status=st.PROCESSING)

    async def process_telegram_file(self, doc_id: int, data: bytes) -> Outcome:
        async with self.lock:
            doc = self.store.get_document(doc_id)
            try:
                extracted = await self.extract(data, doc.filename or "")
            except ExtractionError as exc:
                doc = self.store.update_document(doc.id, status=st.FAILED, status_reason=exc.reason)
                self.store.add_history(doc.id, "extraction_failed", reason=exc.reason)
                return Outcome("failed", doc, message=exc.reason)
            title, alt = self.titles_for("telegram", doc.filename or "", extracted.text)
            path = self.save_file(doc.id, data, doc.filename or "")
            doc = self.store.update_document(
                doc.id, title=title, norm_title=norm_title(title), alt_norm_title=alt, text=extracted.text,
                content_hash=content_hash(extracted.text), extraction_method=extracted.method, storage_path=path)
            self.store.add_history(doc.id, "extracted", method=extracted.method, notes=extracted.notes)
            outcome = await self._after_extraction(doc)
            outcome.notes = extracted.notes
            return outcome

    async def set_scope_and_continue(self, doc_id: int, codes: list[str]) -> Outcome:
        async with self.lock:
            doc = self.store.get_document(doc_id)
            if doc is None or doc.status != st.PENDING_LOCATION:
                raise ValueError("document is not waiting for a location")
            codes = geo.normalize_scope(codes)
            doc = self.store.update_document(doc.id, scope=codes, scope_method="user")
            self.store.add_history(doc.id, "scope", codes=codes, method="user")
            return await self._decide_identity(doc)

    async def resolve_replacement(self, doc_id: int, replaces: bool) -> Outcome:
        async with self.lock:
            doc = self.store.get_document(doc_id)
            if doc is None or doc.status != st.PENDING_REPLACEMENT:
                raise ValueError("document is not waiting for a replacement decision")
            candidate = self.store.get_document(doc.candidate_id) if doc.candidate_id else None
            self.store.add_history(doc.id, "replacement_answer", replaces=replaces, candidate=doc.candidate_id)
            if replaces and candidate is not None and candidate.status == st.ACTIVE:
                return await self._replace(doc, candidate, "підтверджено відправником")
            doc = await self._activate(doc, "окремий документ (за відповіддю відправника)"
                                       if not replaces else "кандидат на заміну вже неактивний")
            return Outcome("activated", doc, candidate)

    def withdraw(self, doc_id: int, user_id: int) -> Document:
        """Withdraw a Telegram upload. Only its uploader (by Telegram user ID) may do this.

        Superseded predecessors are NOT reactivated.
        """
        doc = self.store.get_document(doc_id)
        if doc is None or doc.source != "telegram" or doc.uploader_id != user_id:
            raise PermissionError("not your document")
        if doc.status in (st.WITHDRAWN, st.REMOVED, st.CANCELLED):
            return doc
        was_active = doc.status == st.ACTIVE
        doc = self.store.update_document(doc.id, status=st.WITHDRAWN, status_reason="відкликано відправником")
        self.store.add_history(doc.id, "withdrawn", by=user_id, was_active=was_active)
        self._changed()
        log.info("Document %s withdrawn by user %s", doc.id, user_id)
        return doc

    def cancel_upload(self, doc_id: int, user_id: int) -> None:
        doc = self.store.get_document(doc_id)
        if doc and doc.uploader_id == user_id and doc.status in st.PENDING_STATUSES:
            self.store.update_document(doc.id, status=st.CANCELLED, status_reason="скасовано відправником")
            self.store.add_history(doc.id, "cancelled", by=user_id)

    def recover_interrupted(self) -> None:
        """After a restart: documents left mid-processing are marked failed (Drive retries next sync)."""
        for doc in self.store.documents_by_status(st.PROCESSING):
            self.store.update_document(doc.id, status=st.FAILED,
                                       status_reason="обробку перервано перезапуском; надішліть файл ще раз"
                                       if doc.source == "telegram" else "обробку перервано; повториться під час синхронізації")
            self.store.add_history(doc.id, "interrupted")
            log.warning("Document %s was interrupted during processing; marked failed", doc.id)

    # ------------------------------------------------------------------ Google Drive
    async def ingest_drive_file(self, *, file_id: str, name: str, modified_time: str, link: str,
                                data: bytes | None, error: str | None = None) -> Outcome:
        """Ingest a new or changed Drive file version (caller checked that it is new/changed)."""
        outcome = await self._ingest_drive_file(file_id=file_id, name=name, modified_time=modified_time,
                                                link=link, data=data, error=error)
        # Older unresolved versions of the same file are obsolete once a newer version was processed.
        for older in self.store.documents_for_drive_file(file_id):
            if older.id < outcome.doc.id and older.status in (st.PENDING_LOCATION, st.FAILED):
                self.store.update_document(older.id, status=st.SUPERSEDED, superseded_by=outcome.doc.id,
                                           status_reason=f"оброблено новішу версію файлу (№{outcome.doc.id})")
                self.store.add_history(older.id, "obsolete_file_version", newer=outcome.doc.id)
        return outcome

    async def _ingest_drive_file(self, *, file_id: str, name: str, modified_time: str, link: str,
                                 data: bytes | None, error: str | None = None) -> Outcome:
        async with self.lock:
            previous = [d for d in self.store.documents_for_drive_file(file_id) if d.status != st.REMOVED]
            prev = previous[-1] if previous else None
            doc = self.store.insert_document(
                source="drive", title=nfc(Path(name).stem), norm_title=norm_title(Path(name).stem),
                filename=nfc(name), status=st.PROCESSING, version_ts=modified_time,
                drive_file_id=file_id, drive_link=link,
            )
            self.store.add_history(doc.id, "received", via="drive", modified_time=modified_time,
                                   previous=prev.id if prev else None)
            if error is not None or data is None:
                return self._drive_failed(doc, error or "файл не вдалося завантажити")
            try:
                extracted = await self.extract(data, name)
            except ExtractionError as exc:
                return self._drive_failed(doc, exc.reason)
            title, alt = self.titles_for("drive", name, extracted.text)
            path = self.save_file(doc.id, data, name)
            doc = self.store.update_document(
                doc.id, title=title, norm_title=norm_title(title), alt_norm_title=alt, text=extracted.text,
                content_hash=content_hash(extracted.text), extraction_method=extracted.method, storage_path=path)
            self.store.add_history(doc.id, "extracted", method=extracted.method, notes=extracted.notes)

            # Same Drive file, same content as its last valid version: not a new version. Intermediate
            # failed/pending records (e.g. a temporary loss of download access) are skipped.
            valid = [d for d in previous if d.status in (st.ACTIVE, st.SUPERSEDED, st.DUPLICATE)]
            last_valid = valid[-1] if valid else None
            if last_valid is not None and last_valid.content_hash == doc.content_hash:
                self.store.update_document(doc.id, status=st.DUPLICATE, logical_id=last_valid.logical_id,
                                           status_reason=f"вміст не змінився (версія №{last_valid.id})")
                self.store.add_history(doc.id, "unchanged_content", previous=last_valid.id)
                return Outcome("duplicate", self.store.get_document(doc.id), last_valid)

            scope = await self.resolve_scope(doc.title, doc.text or "")
            if scope is None:
                doc = self.store.update_document(
                    doc.id, status=st.PENDING_LOCATION,
                    status_reason="не вдалося однозначно визначити рукавичку чи частину лісу")
                self.store.add_history(doc.id, "pending_location", previous_active=prev.id if prev else None)
                return Outcome("need_location", doc, prev)
            doc = self.store.update_document(doc.id, scope=geo.normalize_scope(scope.codes), scope_method=scope.method)
            self.store.add_history(doc.id, "scope", codes=doc.scope, method=scope.method,
                                   evidence=scope.evidence[:200])

            # A new version of a known Drive file continues that file's logical document (look past
            # intermediate pending/failed versions of the same file).
            for earlier in reversed(previous):
                chain_active = self._active_in_logical(earlier.logical_id)
                if chain_active is not None:
                    return await self._replace(doc, chain_active, "нова версія того самого файлу на Google Drive")
            return await self._decide_identity(doc)

    def _drive_failed(self, doc: Document, reason: str) -> Outcome:
        doc = self.store.update_document(doc.id, status=st.FAILED, status_reason=reason)
        self.store.add_history(doc.id, "extraction_failed", reason=reason)
        log.warning("Drive document %s «%s» failed: %s", doc.id, doc.title, reason)
        return Outcome("failed", doc, message=reason)

    def remove_drive_file(self, file_id: str) -> list[Document]:
        """Confirmed deletion from the archive: stop using the file; do not restore older versions."""
        removed = []
        for doc in self.store.documents_for_drive_file(file_id):
            if doc.status in (st.ACTIVE, st.PENDING_LOCATION, st.FAILED, st.DUPLICATE, st.PROCESSING):
                was_active = doc.status == st.ACTIVE
                self.store.update_document(doc.id, status=st.REMOVED, status_reason="файл видалено з Лісового архіву")
                self.store.add_history(doc.id, "removed_from_source", was_active=was_active)
                removed.append(doc)
        if removed:
            self._changed()
            log.info("Drive file %s removed: documents %s", file_id, [d.id for d in removed])
        return removed

    def drive_pending(self) -> list[Document]:
        """Latest Drive version per present file that needs clarification or could not be read."""
        result = []
        for f in self.store.drive_files("present"):
            docs = [d for d in self.store.documents_for_drive_file(f["file_id"])]
            if docs and docs[-1].status in (st.PENDING_LOCATION, st.FAILED):
                result.append(docs[-1])
        return sorted(result, key=lambda d: d.title)
