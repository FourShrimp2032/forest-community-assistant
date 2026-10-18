"""Application assembly: builds services from Settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .conversation import Conversation
from .drive import DriveClient, DriveSync
from .embeddings import Embedder
from .library import Library
from .llm import GroqClient
from .qa import QAService
from .retrieval import Retriever
from .scope import build_llm_scope_messages, validate_llm_scope
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    store: Store
    embedder: Embedder
    llm: GroqClient | None
    library: Library
    retriever: Retriever
    qa: QAService
    conversation: Conversation
    drive_sync: DriveSync | None
    drive_client: DriveClient | None

    async def close(self) -> None:
        if self.llm:
            await self.llm.close()
        if self.drive_client:
            await self.drive_client.close()
        self.store.close()


def build_services(settings: Settings) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    embedder = Embedder(settings.embedding_model, settings.embedding_cache_dir, settings.embeddings_enabled)

    llm = None
    if settings.groq_api_key:
        llm = GroqClient(settings.groq_api_key, base_url=settings.groq_base_url,
                         timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
                         max_retry_wait=settings.llm_max_retry_wait_seconds,
                         reasoning_effort=settings.llm_reasoning_effort,
                         max_concurrency=settings.llm_max_concurrency,
                         fallback_models=settings.llm_fallback_models,
                         fallback_wait=settings.llm_fallback_wait_seconds)

    async def classify_scope(title: str, text: str):
        if llm is None:
            return None
        raw = await llm.chat_json(settings.llm_router_model, build_llm_scope_messages(title, text),
                                  max_tokens=400, temperature=0)
        return validate_llm_scope(raw, title, text)

    library = Library(store, settings.files_dir, settings.ocr, classify_scope if llm else None, embedder,
                      settings.chunk_max_chars)
    retriever = Retriever(store, embedder, max_docs=settings.retrieval_max_docs,
                          per_target_docs=settings.retrieval_per_target_docs,
                          evidence_chars=settings.retrieval_evidence_chars)
    qa = QAService(llm, retriever, model=settings.llm_model, router_model=settings.llm_router_model,
                   tz=settings.tz, answer_max_tokens=settings.llm_answer_max_tokens,
                   temperature=settings.llm_temperature, history_turns=settings.history_turns,
                   answer_reasoning_effort=settings.llm_answer_reasoning_effort)
    conversation = Conversation(store, library, qa, settings.tz, max_upload_mb=settings.max_upload_mb,
                                page_size=settings.list_page_size, history_turns=settings.history_turns,
                                request_budget_seconds=settings.request_budget_seconds,
                                archive_sync_enabled=bool(settings.google_drive_api_key and settings.drive_sync_enabled))

    drive_client = drive_sync = None
    if settings.google_drive_api_key and settings.drive_sync_enabled:
        drive_client = DriveClient(settings.google_drive_api_key, timeout=settings.drive_timeout_seconds,
                                   recursive=settings.drive_recursive)
        drive_sync = DriveSync(drive_client, store, library, settings.google_drive_folder_id,
                               settings.drive_max_delete_fraction, settings.drive_download_delay_seconds)
    return Services(settings, store, embedder, llm, library, retriever, qa, conversation, drive_sync, drive_client)


async def startup_checks(services: Services) -> None:
    services.library.recover_interrupted()
    if services.settings.snapshot_bootstrap:
        from .snapshot import bootstrap
        result = await bootstrap(services.store, services.library, services.settings.snapshot_dir,
                                 services.settings.google_drive_folder_id)
        if result.imported:
            log.info("Local archive snapshot ready: %d documents (remote freshness not yet verified)",
                     result.imported)
        else:
            log.info("Bootstrap snapshot not imported: %s", result.skipped)
    await services.library.reindex_missing_embeddings()
    services.retriever.refresh(services.library.revision, force=True)
    if services.llm is not None:
        try:
            models = await services.llm.list_models()
            for model in {services.settings.llm_model, services.settings.llm_router_model,
                          *services.settings.llm_fallback_models}:
                if model in models:
                    log.info("Groq model available: %s", model)
                else:
                    log.error("Configured Groq model %r is NOT available for this API key. Available: %s",
                              model, ", ".join(sorted(models)))
        except Exception as exc:
            if "401" in str(exc):
                log.error("Groq rejected GROQ_API_KEY (401 Unauthorized): answers will fail until it is fixed")
            else:
                log.warning("Could not list Groq models: %s", exc)
    if services.drive_sync is None:
        log.warning("Google Drive sync is DISABLED (GOOGLE_DRIVE_API_KEY not set or DRIVE_SYNC_ENABLED=false). "
                    "Only documents already stored and Telegram uploads are available.")
