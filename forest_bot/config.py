"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .extraction import OcrSettings


class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    telegram_bot_token: str = ""
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_router_model: str = "openai/gpt-oss-20b"
    llm_fallback_models: list[str] = field(default_factory=lambda: ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"])
    llm_reasoning_effort: str | None = "low"
    llm_answer_reasoning_effort: str | None = "medium"
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_max_retry_wait_seconds: float = 30.0
    llm_fallback_wait_seconds: float = 2.0
    request_budget_seconds: float = 45.0
    llm_max_concurrency: int = 2
    llm_answer_max_tokens: int = 3000

    google_drive_api_key: str = ""
    google_drive_folder_id: str = "1b_hEjnkQfP8ZVP-OEbnYWqoGSFwXMAP3"
    drive_sync_interval_seconds: int = 600
    drive_sync_enabled: bool = True
    drive_timeout_seconds: float = 60.0
    drive_max_delete_fraction: float = 0.5
    drive_recursive: bool = True
    drive_download_delay_seconds: float = 2.0
    drive_throttle_retry_seconds: int = 300
    drive_throttle_max_retry_seconds: int = 3600

    data_dir: Path = Path("/data")
    timezone_name: str = "Europe/Kyiv"

    embeddings_enabled: bool = True
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_cache_dir: str | None = None

    retrieval_max_docs: int = 8
    retrieval_per_target_docs: int = 4
    retrieval_evidence_chars: int = 7000
    chunk_max_chars: int = 900
    history_turns: int = 3

    max_upload_mb: int = 20
    list_page_size: int = 5
    ocr: OcrSettings = field(default_factory=OcrSettings)
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "forest.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.telegram_bot_token = _env("TELEGRAM_BOT_TOKEN", "")
        s.groq_api_key = _env("GROQ_API_KEY", "")
        s.groq_base_url = _env("GROQ_BASE_URL", s.groq_base_url)
        s.llm_model = _env("GROQ_MODEL", s.llm_model)
        s.llm_router_model = _env("GROQ_ROUTER_MODEL", s.llm_router_model)
        fallbacks = _env("GROQ_FALLBACK_MODELS")
        if fallbacks is not None:
            s.llm_fallback_models = [m.strip() for m in fallbacks.split(",") if m.strip() and m.strip() != "none"]
        effort = _env("GROQ_REASONING_EFFORT", s.llm_reasoning_effort)
        s.llm_reasoning_effort = None if effort in (None, "none", "off") else effort
        answer_effort = _env("GROQ_ANSWER_REASONING_EFFORT", s.llm_answer_reasoning_effort)
        s.llm_answer_reasoning_effort = None if answer_effort in (None, "none", "off") else answer_effort
        s.llm_temperature = _float("GROQ_TEMPERATURE", s.llm_temperature)
        s.llm_timeout_seconds = _float("GROQ_TIMEOUT_SECONDS", s.llm_timeout_seconds)
        s.llm_max_retries = _int("GROQ_MAX_RETRIES", s.llm_max_retries)
        s.llm_max_retry_wait_seconds = _float("GROQ_MAX_RETRY_WAIT_SECONDS", s.llm_max_retry_wait_seconds)
        s.llm_fallback_wait_seconds = _float("GROQ_FALLBACK_WAIT_SECONDS", s.llm_fallback_wait_seconds)
        s.request_budget_seconds = _float("REQUEST_BUDGET_SECONDS", s.request_budget_seconds)
        s.llm_max_concurrency = _int("GROQ_MAX_CONCURRENCY", s.llm_max_concurrency)
        s.llm_answer_max_tokens = _int("GROQ_ANSWER_MAX_TOKENS", s.llm_answer_max_tokens)

        s.google_drive_api_key = _env("GOOGLE_DRIVE_API_KEY", "")
        s.google_drive_folder_id = _env("GOOGLE_DRIVE_FOLDER_ID", s.google_drive_folder_id)
        s.drive_sync_interval_seconds = _int("DRIVE_SYNC_INTERVAL_SECONDS", s.drive_sync_interval_seconds)
        s.drive_sync_enabled = _bool("DRIVE_SYNC_ENABLED", s.drive_sync_enabled)
        s.drive_timeout_seconds = _float("DRIVE_TIMEOUT_SECONDS", s.drive_timeout_seconds)
        s.drive_max_delete_fraction = _float("DRIVE_MAX_DELETE_FRACTION", s.drive_max_delete_fraction)
        s.drive_recursive = _bool("DRIVE_RECURSIVE", s.drive_recursive)
        s.drive_download_delay_seconds = _float("DRIVE_DOWNLOAD_DELAY_SECONDS", s.drive_download_delay_seconds)
        s.drive_throttle_retry_seconds = _int("DRIVE_THROTTLE_RETRY_SECONDS", s.drive_throttle_retry_seconds)
        s.drive_throttle_max_retry_seconds = _int("DRIVE_THROTTLE_MAX_RETRY_SECONDS",
                                                  s.drive_throttle_max_retry_seconds)

        s.data_dir = Path(_env("DATA_DIR", str(s.data_dir)))
        s.timezone_name = _env("TIMEZONE", s.timezone_name)

        s.embeddings_enabled = _bool("EMBEDDINGS_ENABLED", s.embeddings_enabled)
        s.embedding_model = _env("EMBEDDING_MODEL", s.embedding_model)
        s.embedding_cache_dir = _env("FASTEMBED_CACHE_DIR", None)

        s.retrieval_max_docs = _int("RETRIEVAL_MAX_DOCS", s.retrieval_max_docs)
        s.retrieval_per_target_docs = _int("RETRIEVAL_PER_TARGET_DOCS", s.retrieval_per_target_docs)
        s.retrieval_evidence_chars = _int("RETRIEVAL_EVIDENCE_CHARS", s.retrieval_evidence_chars)
        s.chunk_max_chars = _int("CHUNK_MAX_CHARS", s.chunk_max_chars)
        s.history_turns = _int("HISTORY_TURNS", s.history_turns)

        s.max_upload_mb = _int("MAX_UPLOAD_MB", s.max_upload_mb)
        s.list_page_size = _int("LIST_PAGE_SIZE", s.list_page_size)
        s.ocr = OcrSettings(
            enabled=_bool("OCR_ENABLED", True),
            language=_env("OCR_LANGUAGE", "ukr"),
            dpi=_int("OCR_DPI", 300),
            min_confidence=_float("OCR_MIN_CONFIDENCE", 55.0),
            timeout_seconds=_int("OCR_TIMEOUT_SECONDS", 90),
            max_pages=_int("MAX_PDF_PAGES", 30),
            binarize_threshold=_int("OCR_BINARIZE_THRESHOLD", 110),
        )
        s.log_level = _env("LOG_LEVEL", s.log_level).upper()
        try:
            ZoneInfo(s.timezone_name)
        except Exception:
            raise ConfigError(f"Unknown TIMEZONE {s.timezone_name!r}")
        return s

    def validate_for_bot(self) -> list[str]:
        """Return a list of fatal configuration problems."""
        problems = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.groq_api_key:
            problems.append("GROQ_API_KEY is not set")
        return problems
