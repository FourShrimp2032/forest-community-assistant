"""Transport-independent dialogue logic. The Telegram adapter turns `Reply` objects into messages."""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from . import geography as geo
from . import messages as msg
from . import storage as st
from . import timing
from .extraction import SUPPORTED_EXTENSIONS, extension_of
from .library import Library, Outcome
from .llm import LLMRateLimited
from .qa import ARCHIVE_NAME, Analysis, QAService, format_local_time
from .retrieval import QueryScope
from .storage import Document, Store

log = logging.getLogger(__name__)

Downloader = Callable[[str], Awaitable[bytes]]


@dataclass
class Button:
    label: str
    data: str | None = None
    url: str | None = None


@dataclass
class Reply:
    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    document_file_id: str | None = None     # send a stored Telegram file
    edit: bool = False                      # edit the message the callback came from
    toast: str | None = None                # short callback notification


def _rows(buttons: list[Button], per_row: int = 2) -> list[list[Button]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def looks_like_name(text: str) -> bool:
    t = text.strip()
    if not 2 <= len(t) <= 60 or t.startswith("/"):
        return False
    if any(ch.isdigit() for ch in t) or "?" in t:
        return False
    if len(t.split()) > 4:
        return False
    if geo.detect_locations(t).any:
        return False
    return bool(re.fullmatch(r"[^\W\d_]+(?:[ '\-’ʼ.][^\W\d_]+)*\.?", t))


def source_description(doc: Document, tz: ZoneInfo) -> str:
    if doc.source == "drive":
        return ARCHIVE_NAME
    return f"надіслано: {doc.uploader_name or 'невідомо'}, {format_local_time(doc.version_dt, tz)}"


class Conversation:
    def __init__(self, store: Store, library: Library, qa: QAService, tz: ZoneInfo, *,
                 max_upload_mb: int = 20, page_size: int = 5, history_turns: int = 3,
                 request_budget_seconds: float = 45.0, archive_sync_enabled: bool = False):
        self.store = store
        self.library = library
        self.qa = qa
        self.tz = tz
        self.max_upload_mb = max_upload_mb
        self.page_size = page_size
        self.history_turns = history_turns
        self.request_budget_seconds = request_budget_seconds
        self.archive_sync_enabled = archive_sync_enabled
        self._user_locks: dict[int, asyncio.Lock] = {}
        self.downloader: Downloader | None = None   # set by the transport: Telegram file_id -> bytes

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    # ------------------------------------------------------------------ state helpers
    def _state(self, user_id: int) -> dict:
        state = self.store.get_state(user_id)
        state.setdefault("history", [])
        state.setdefault("queue", [])
        state.setdefault("loc_sel", {})
        return state

    def _save(self, user_id: int, state: dict) -> None:
        self.store.save_state(user_id, state)

    def _queue_head(self, user_id: int, state: dict) -> Document | None:
        """First upload of this user that still waits for an answer; drops resolved ones."""
        changed = False
        while state["queue"]:
            doc = self.store.get_document(state["queue"][0])
            if doc and doc.uploader_id == user_id and doc.status in (
                    st.AWAITING_NAME, st.PENDING_LOCATION, st.PENDING_REPLACEMENT):
                if changed:
                    self._save(user_id, state)
                return doc
            state["queue"].pop(0)
            changed = True
        if changed:
            self._save(user_id, state)
        return None

    # ------------------------------------------------------------------ commands
    async def on_command(self, user_id: int, command: str, args: str = "") -> list[Reply]:
        command = command.lower().lstrip("/").split("@")[0]
        if command == "start":
            return [Reply(msg.START)]
        if command == "help":
            return [Reply(msg.HELP)]
        if command == "reset":
            state = self._state(user_id)
            state.update({"scope": None, "history": [], "awaiting": None, "context": {}})
            self._save(user_id, state)
            text = msg.RESET_DONE + (msg.RESET_PENDING_UPLOADS if self._queue_head(user_id, state) else "")
            return [Reply(text)]
        if command in ("my_documents", "mydocuments"):
            return [self._my_documents_page(user_id, 0)]
        if command == "pending":
            return [self._pending_page(0)]
        return [Reply(msg.HELP)]

    # ------------------------------------------------------------------ text
    async def on_text(self, user_id: int, text: str) -> list[Reply]:
        text = text.strip()
        if not text:
            return []
        state = self._state(user_id)
        head = self._queue_head(user_id, state)

        if head is not None and head.status == st.AWAITING_NAME:
            if looks_like_name(text):
                return await self._accept_name(user_id, text)
            replies = await self._handle_question(user_id, text)
            if replies:
                replies[-1].text += msg.NAME_REMINDER
            return replies

        if head is not None and head.status == st.PENDING_LOCATION:
            codes = geo.parse_location_answer(text)
            if codes and len(text) <= 80 and "?" not in text:
                return await self._set_doc_location(user_id, head.id, codes)

        if head is not None and head.status == st.PENDING_REPLACEMENT:
            norm = geo.normalize(text).strip(" .!")
            if norm in ("замінює", "заміна", "так", "нова версія", "замінити"):
                return await self._resolve_replacement(user_id, head.id, True)
            if norm in ("окремий", "окремий документ", "ні", "додатковий", "доповнення"):
                return await self._resolve_replacement(user_id, head.id, False)

        awaiting = state.get("awaiting") or {}
        if awaiting.get("type") == "question_location":
            codes = geo.parse_location_answer(text)
            if codes and len(text) <= 80 and "?" not in text:
                return await self._answer_with_location(user_id, codes)

        replies = await self._handle_question(user_id, text)
        head = self._queue_head(user_id, self._state(user_id))
        if head is not None and replies:
            replies[-1].text += msg.CLARIFICATION_REMINDER.format(title=html.escape(head.title))
        return replies

    async def _handle_question(self, user_id: int, text: str) -> list[Reply]:
        with timing.request(self.request_budget_seconds) as t:
            replies = await self._handle_question_timed(user_id, text)
            log.info("Timing: %s", t.summary())
        return replies

    async def _handle_question_timed(self, user_id: int, text: str) -> list[Reply]:
        state = self._state(user_id)
        previous = QueryScope.from_state(state.get("scope"))
        try:
            analysis = await self.qa.analyze(text, previous, state["history"], state.get("context"))
        except LLMRateLimited:
            return [Reply(msg.RATE_LIMITED)]
        if analysis.intent == "offtopic":
            return [Reply(msg.OFFTOPIC)]
        if analysis.intent == "greeting":
            return [Reply(msg.GREETING)]
        if analysis.scope is None:
            state["awaiting"] = {"type": "question_location", "question": text,
                                 "search_query": analysis.search_query}
            self._save(user_id, state)
            return [Reply(msg.ASK_QUESTION_LOCATION, self._question_location_keyboard())]
        state["awaiting"] = None
        state["context"] = analysis.persist_context if analysis.persist_context is not None else analysis.context
        self._save(user_id, state)
        return await self._answer(user_id, text, analysis)

    async def _answer(self, user_id: int, question: str, analysis) -> list[Reply]:
        state = self._state(user_id)
        try:
            result = await self.qa.answer(question, analysis, state["history"], self.library.revision)
        except LLMRateLimited:
            return [Reply(msg.RATE_LIMITED)]
        except Exception as exc:
            log.exception("Answer generation failed: %s", exc)
            return [Reply(msg.THINKING_ERROR)]
        t = timing.current()
        if t is not None:
            t.status, t.evidence_docs = result.status, result.evidence_count
        result = self._with_archive_notice(result)
        if analysis.scope is not None and analysis.scope.kind != "general":
            state["scope"] = analysis.scope.to_state()
        turn = {"q": question[:500], "resolved": (analysis.focus or question)[:500],
                "search": (analysis.search_query or "")[:500], "a": result.plain[:500]}
        state["history"] = (state["history"] + [turn])[-self.history_turns:]
        self._save(user_id, state)
        buttons = [[Button(label, data=f"f:{doc_id}")] for doc_id, label in result.file_buttons]
        return [Reply(result.html, buttons)]

    def _with_archive_notice(self, result):
        """While the initial archive is incomplete, never imply that the whole archive was searched."""
        from .drive import archive_status
        status = archive_status(self.store, self.archive_sync_enabled)
        if status.snapshot_unverified:
            date = status.snapshot_date
            if result.status == "not_found":
                text = msg.NOT_FOUND_SNAPSHOT_UNVERIFIED.format(date=date)
                result.html, result.plain = text, text
            elif result.status == "answered":
                result.html = f"{msg.SNAPSHOT_UNVERIFIED.format(date=date)}\n\n{result.html}"
            return result
        if not status.loading:
            return result
        known = status.listed_once and status.total
        if result.status == "not_found":
            text = (msg.NOT_FOUND_WHILE_LOADING.format(waiting=status.waiting, total=status.total) if known
                    else msg.NOT_FOUND_WHILE_LOADING_UNKNOWN)
            result.html, result.plain = text, text
        elif result.status == "answered":
            notice = (msg.ARCHIVE_LOADING.format(waiting=status.waiting, total=status.total) if known
                      else msg.ARCHIVE_LOADING_UNKNOWN)
            result.html = f"{notice}\n\n{result.html}"
        return result

    async def _answer_with_location(self, user_id: int, codes: list[str]) -> list[Reply]:
        state = self._state(user_id)
        awaiting = state.get("awaiting") or {}
        question = awaiting.get("question")
        if not question:
            return [Reply(msg.GREETING)]
        mittens = [c for c in codes if geo.is_mitten(c)]
        regions = [c for c in codes if geo.is_region(c)]
        if geo.FOREST in codes:
            scope = QueryScope("forest", comparison=True)
        elif mittens:
            scope = QueryScope("mittens", mittens, [], len(mittens) > 1)
        else:
            scope = QueryScope("regions", [], regions, len(regions) > 1)
        from . import dialogue
        context = dialogue.merge(state.get("context"), dialogue.extract(question))
        analysis = Analysis("question", scope, False, awaiting.get("search_query") or question, "explicit",
                            focus=question, context=context)
        state["awaiting"] = None
        state["context"] = context
        self._save(user_id, state)
        with timing.request(self.request_budget_seconds) as t:
            replies = await self._answer(user_id, question, analysis)
            log.info("Timing: %s", t.summary())
        return replies

    def _question_location_keyboard(self) -> list[list[Button]]:
        buttons = [Button(name, data=f"ql:{code}") for code, name in geo.MITTENS.items()]
        rows = _rows(buttons, 2)
        rows.append([Button("🌲 Увесь ліс (порівняти)", data="ql:forest")])
        return rows

    # ------------------------------------------------------------------ uploads
    async def on_document(self, user_id: int, chat_id: int, file_id: str, filename: str, size: int | None,
                          received_at: datetime) -> list[Reply]:
        filename = filename or "document"
        ext = extension_of(filename)
        if ext not in SUPPORTED_EXTENSIONS:
            return [Reply(msg.UNSUPPORTED_FORMAT.format(ext=html.escape(ext or filename)))]
        if size and size > self.max_upload_mb * 1024 * 1024:
            return [Reply(msg.FILE_TOO_LARGE.format(size=math.ceil(size / 1024 / 1024), limit=self.max_upload_mb))]
        user = self.store.get_user(user_id)
        doc = self.library.create_telegram_upload(user_id=user_id, chat_id=chat_id, tg_file_id=file_id,
                                                  filename=filename, received_at=received_at,
                                                  uploader_name=user.get("real_name"))
        if not user.get("real_name"):
            state = self._state(user_id)
            already_asked = any(
                (d := self.store.get_document(i)) and d.status == st.AWAITING_NAME for i in state["queue"])
            state["queue"].append(doc.id)
            self._save(user_id, state)
            return [Reply(msg.ASK_NAME_AGAIN if already_asked else msg.ASK_NAME)]
        return await self._process_upload(user_id, doc)

    async def _accept_name(self, user_id: int, text: str) -> list[Reply]:
        name = re.sub(r"\s+", " ", text.strip()).rstrip(".")
        self.store.set_user_name(user_id, name)
        replies = [Reply(msg.NAME_SAVED.format(name=html.escape(name)))]
        state = self._state(user_id)
        waiting = [i for i in state["queue"]
                   if (d := self.store.get_document(i)) and d.status == st.AWAITING_NAME and d.uploader_id == user_id]
        state["queue"] = [i for i in state["queue"] if i not in waiting]
        self._save(user_id, state)
        for doc_id in waiting:
            doc = self.library.set_uploader_name(doc_id, name)
            replies += await self._process_upload(user_id, doc)
        return replies

    async def _process_upload(self, user_id: int, doc: Document) -> list[Reply]:
        replies = [Reply(msg.PROCESSING.format(name=html.escape(doc.filename or doc.title)))]
        try:
            if self.downloader is None:
                raise RuntimeError("no downloader configured")
            data = await self.downloader(doc.tg_file_id)
        except Exception as exc:
            log.warning("Telegram download failed for document %s: %s", doc.id, exc)
            self.store.update_document(doc.id, status=st.FAILED, status_reason="не вдалося завантажити з Telegram")
            replies.append(Reply(msg.DOWNLOAD_FAILED))
            return replies
        outcome = await self.library.process_telegram_file(doc.id, data)
        replies += self._outcome_replies(user_id, outcome)
        return replies

    def _outcome_replies(self, user_id: int, outcome: Outcome) -> list[Reply]:
        doc = outcome.doc
        title = html.escape(doc.title)
        extra = ""
        if doc.extraction_method in ("pdf-ocr", "pdf-mixed"):
            extra += msg.OCR_NOTE
        uncertain = [n for n in outcome.notes if "неоднозначно" in n]
        if uncertain:
            extra += msg.OCR_UNCERTAIN.format(notes=html.escape("; ".join(uncertain)))
        scope = html.escape(geo.scope_label(doc.scope or []))
        if outcome.kind in ("activated", "kept_separate"):
            return [Reply(msg.ACTIVATED.format(title=title, scope=scope) + extra)]
        if outcome.kind == "replaced":
            old = outcome.other
            return [Reply(msg.REPLACED.format(title=title, old_title=html.escape(old.title),
                                              old_source=html.escape(source_description(old, self.tz)),
                                              scope=scope) + extra)]
        if outcome.kind == "older":
            old = outcome.other
            return [Reply(msg.OLDER.format(title=title, old_title=html.escape(old.title),
                                           old_source=html.escape(source_description(old, self.tz))))]
        if outcome.kind == "duplicate":
            other = outcome.other
            return [Reply(msg.DUPLICATE.format(title=html.escape(other.title),
                                               source=html.escape(source_description(other, self.tz))))]
        if outcome.kind == "failed":
            return [Reply(msg.EXTRACTION_FAILED.format(name=html.escape(doc.filename or doc.title),
                                                       reason=html.escape(outcome.message)))]
        if outcome.kind in ("need_location", "need_replacement"):
            state = self._state(user_id)
            if doc.id not in state["queue"]:
                state["queue"].append(doc.id)
                self._save(user_id, state)
            head = self._queue_head(user_id, state)
            if head is not None and head.id != doc.id:
                return [Reply(f"Документ «{title}» теж потребує уточнення — спитаю після попереднього." + extra)]
            return [self._clarification_prompt(user_id, doc, extra)]
        return [Reply(f"Документ «{title}»: {html.escape(outcome.kind)}")]

    def _clarification_prompt(self, user_id: int, doc: Document, extra: str = "") -> Reply:
        title = html.escape(doc.title)
        if doc.status == st.PENDING_LOCATION:
            return Reply(msg.ASK_DOC_LOCATION.format(title=title) + extra, self._doc_location_keyboard(user_id, doc.id))
        if doc.status == st.PENDING_REPLACEMENT:
            old = self.store.get_document(doc.candidate_id)
            text = msg.ASK_REPLACEMENT.format(
                title=title, old_title=html.escape(old.title),
                old_source=html.escape(source_description(old, self.tz)),
                old_date=format_local_time(old.version_dt, self.tz),
                old_scope=html.escape(geo.scope_label(old.scope or [])),
                reason=html.escape(doc.status_reason or ""))
            return Reply(text + extra, [[Button("🔁 Замінює", data=f"rp:y:{doc.id}"),
                                         Button("➕ Окремий документ", data=f"rp:n:{doc.id}")],
                                        [Button("✖️ Скасувати завантаження", data=f"rp:x:{doc.id}")]])
        if doc.status == st.AWAITING_NAME:
            return Reply(msg.ASK_NAME)
        return Reply("")

    def _doc_location_keyboard(self, user_id: int, doc_id: int) -> list[list[Button]]:
        selected = set(self._state(user_id)["loc_sel"].get(str(doc_id), []))

        def b(code: str, label: str) -> Button:
            return Button(("✅ " if code in selected else "") + label, data=f"dl:t:{doc_id}:{code}")

        rows = _rows([b(c, n) for c, n in geo.MITTENS.items()], 2)
        rows += _rows([b(c, n) for c, n in geo.REGIONS.items()], 3)
        rows.append([b(geo.FOREST, "🌲 Увесь ліс")])
        rows.append([Button("✔️ Готово", data=f"dl:ok:{doc_id}"),
                     Button("✖️ Скасувати", data=f"dl:x:{doc_id}")])
        return rows

    async def _set_doc_location(self, user_id: int, doc_id: int, codes: list[str]) -> list[Reply]:
        doc = self.store.get_document(doc_id)
        if doc is None or doc.uploader_id != user_id or doc.status != st.PENDING_LOCATION:
            return [Reply(FILE_STATE_CHANGED)]
        state = self._state(user_id)
        state["loc_sel"].pop(str(doc_id), None)
        self._save(user_id, state)
        outcome = await self.library.set_scope_and_continue(doc_id, codes)
        replies = [Reply(msg.DOC_LOCATION_SET.format(title=html.escape(doc.title),
                                                     scope=html.escape(geo.scope_label(outcome.doc.scope or []))))]
        replies += self._outcome_replies(user_id, outcome)
        replies += self._next_clarification(user_id, exclude=doc_id)
        return replies

    async def _resolve_replacement(self, user_id: int, doc_id: int, replaces: bool) -> list[Reply]:
        doc = self.store.get_document(doc_id)
        if doc is None or doc.uploader_id != user_id or doc.status != st.PENDING_REPLACEMENT:
            return [Reply(FILE_STATE_CHANGED)]
        outcome = await self.library.resolve_replacement(doc_id, replaces)
        return self._outcome_replies(user_id, outcome) + self._next_clarification(user_id, exclude=doc_id)

    def _next_clarification(self, user_id: int, exclude: int) -> list[Reply]:
        head = self._queue_head(user_id, self._state(user_id))
        if head is None or head.id == exclude:
            return []
        return [self._clarification_prompt(user_id, head)]

    # ------------------------------------------------------------------ callbacks
    async def on_callback(self, user_id: int, data: str) -> list[Reply]:
        parts = data.split(":")
        kind = parts[0]
        try:
            if kind == "ql":
                state = self._state(user_id)
                if not (state.get("awaiting") or {}).get("question"):
                    return [Reply(toast="Питання вже неактуальне — поставте його ще раз.")]
                return await self._answer_with_location(user_id, [parts[1]])
            if kind == "dl":
                return await self._doc_location_callback(user_id, parts)
            if kind == "rp":
                doc_id = int(parts[2])
                if parts[1] == "x":
                    return self._cancel_upload(user_id, doc_id)
                return await self._resolve_replacement(user_id, doc_id, parts[1] == "y")
            if kind == "f":
                return self._send_file(int(parts[1]))
            if kind == "md":
                return self._my_documents_callback(user_id, parts)
            if kind == "pd":
                return [self._pending_page(int(parts[2]), edit=True)]
        except (ValueError, IndexError) as exc:
            log.warning("Bad callback data %r: %s", data, exc)
        return [Reply(toast="Невідома дія")]

    async def _doc_location_callback(self, user_id: int, parts: list[str]) -> list[Reply]:
        action, doc_id = parts[1], int(parts[2])
        doc = self.store.get_document(doc_id)
        if doc is None or doc.uploader_id != user_id or doc.status != st.PENDING_LOCATION:
            return [Reply(toast="Цей документ уже не чекає уточнення")]
        state = self._state(user_id)
        key = str(doc_id)
        if action == "t":
            code = parts[3]
            selected = set(state["loc_sel"].get(key, []))
            selected ^= {code}
            state["loc_sel"][key] = geo.sort_codes(selected)
            self._save(user_id, state)
            return [Reply(msg.ASK_DOC_LOCATION.format(title=html.escape(doc.title)),
                          self._doc_location_keyboard(user_id, doc_id), edit=True)]
        if action == "ok":
            codes = state["loc_sel"].get(key, [])
            if not codes:
                return [Reply(toast=msg.DOC_LOCATION_EMPTY)]
            return await self._set_doc_location(user_id, doc_id, codes)
        if action == "x":
            return self._cancel_upload(user_id, doc_id)
        return [Reply(toast="Невідома дія")]

    def _cancel_upload(self, user_id: int, doc_id: int) -> list[Reply]:
        doc = self.store.get_document(doc_id)
        if doc is None or doc.uploader_id != user_id:
            return [Reply(toast=msg.NOT_YOUR_DOCUMENT)]
        self.library.cancel_upload(doc_id, user_id)
        state = self._state(user_id)
        state["queue"] = [i for i in state["queue"] if i != doc_id]
        state["loc_sel"].pop(str(doc_id), None)
        self._save(user_id, state)
        return [Reply(msg.UPLOAD_CANCELLED.format(title=html.escape(doc.title)))] + \
            self._next_clarification(user_id, exclude=doc_id)

    def _send_file(self, doc_id: int) -> list[Reply]:
        doc = self.store.get_document(doc_id)
        if doc is None or doc.source != "telegram" or not doc.tg_file_id:
            return [Reply(msg.FILE_UNAVAILABLE)]
        if doc.status == st.WITHDRAWN:
            return [Reply(msg.FILE_WITHDRAWN)]
        if doc.status in (st.CANCELLED, st.REMOVED):
            return [Reply(msg.FILE_UNAVAILABLE)]
        return [Reply(document_file_id=doc.tg_file_id)]

    # ------------------------------------------------------------------ /my_documents
    def _my_documents_page(self, user_id: int, page: int, edit: bool = False) -> Reply:
        docs = [d for d in self.store.documents_by_uploader(user_id) if d.status != st.CANCELLED]
        if not docs:
            return Reply(msg.MY_DOCS_EMPTY, edit=edit)
        pages = max(1, math.ceil(len(docs) / self.page_size))
        page = min(max(page, 0), pages - 1)
        chunk = docs[page * self.page_size:(page + 1) * self.page_size]
        lines = [msg.MY_DOCS_HEADER.format(page=page + 1, pages=pages)]
        buttons: list[list[Button]] = []
        for d in chunk:
            status = msg.STATUS_LABELS.get(d.status, d.status)
            scope = f" · 📍 {html.escape(geo.scope_label(d.scope))}" if d.scope else ""
            lines.append(f"\n<b>№{d.id}</b> «{html.escape(d.title)}»\n"
                         f"{format_local_time(d.version_dt, self.tz)} · {status}{scope}")
            if d.status not in (st.WITHDRAWN, st.REMOVED, st.CANCELLED):
                buttons.append([Button(f"Відкликати №{d.id}", data=f"md:w:{d.id}:{page}")])
        nav = []
        if page > 0:
            nav.append(Button("◀️ Назад", data=f"md:p:{page - 1}"))
        if page < pages - 1:
            nav.append(Button("Далі ▶️", data=f"md:p:{page + 1}"))
        if nav:
            buttons.append(nav)
        return Reply("\n".join(lines), buttons, edit=edit)

    def _my_documents_callback(self, user_id: int, parts: list[str]) -> list[Reply]:
        action = parts[1]
        if action == "p":
            return [self._my_documents_page(user_id, int(parts[2]), edit=True)]
        doc_id = int(parts[2])
        doc = self.store.get_document(doc_id)
        if doc is None or doc.source != "telegram" or doc.uploader_id != user_id:
            return [Reply(msg.NOT_YOUR_DOCUMENT)]
        page = int(parts[3]) if len(parts) > 3 else 0
        if action == "w":
            return [Reply(msg.WITHDRAW_CONFIRM.format(title=html.escape(doc.title)),
                          [[Button("Так, відкликати", data=f"md:wy:{doc_id}:{page}"),
                            Button("Ні", data=f"md:wn:{doc_id}:{page}")]])]
        if action == "wy":
            try:
                doc = self.library.withdraw(doc_id, user_id)
            except PermissionError:
                return [Reply(msg.NOT_YOUR_DOCUMENT)]
            state = self._state(user_id)
            state["queue"] = [i for i in state["queue"] if i != doc_id]
            self._save(user_id, state)
            return [Reply(msg.WITHDRAWN_OK.format(title=html.escape(doc.title)), edit=True)]
        if action == "wn":
            return [Reply(msg.WITHDRAW_CANCELLED, edit=True)]
        return [Reply(toast="Невідома дія")]

    # ------------------------------------------------------------------ /pending
    def _pending_page(self, page: int, edit: bool = False) -> Reply:
        docs = self.library.drive_pending()
        if not docs:
            return Reply(msg.PENDING_EMPTY, edit=edit)
        pages = max(1, math.ceil(len(docs) / self.page_size))
        page = min(max(page, 0), pages - 1)
        lines = [msg.PENDING_HEADER.format(page=page + 1, pages=pages)]
        for d in docs[page * self.page_size:(page + 1) * self.page_size]:
            link = html.escape(d.drive_link or "", quote=True)
            lines.append(f"\n• «{html.escape(d.title)}» — <a href=\"{link}\">файл</a>\n"
                         f"  Причина: {html.escape(d.status_reason or msg.STATUS_LABELS.get(d.status, d.status))}")
        lines.append(msg.PENDING_FOOTER)
        nav = []
        if page > 0:
            nav.append(Button("◀️ Назад", data=f"pd:p:{page - 1}"))
        if page < pages - 1:
            nav.append(Button("Далі ▶️", data=f"pd:p:{page + 1}"))
        return Reply("\n".join(lines), [nav] if nav else [], edit=edit)


FILE_STATE_CHANGED = "Цей документ уже не чекає уточнення."
