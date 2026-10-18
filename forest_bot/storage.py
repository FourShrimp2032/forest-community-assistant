"""SQLite persistence: document versions, chunks, Drive file state, users, sync runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Document lifecycle statuses.
AWAITING_NAME = "awaiting_name"            # Telegram upload waiting for the uploader's real name
PROCESSING = "processing"                  # being downloaded / extracted
PENDING_LOCATION = "pending_location"      # scope unclear; not used in answers
PENDING_REPLACEMENT = "pending_replacement"  # Telegram: waiting for "replaces or separate?"
FAILED = "failed"                          # unreadable / unsupported; not used
ACTIVE = "active"                          # used in answers
SUPERSEDED = "superseded"                  # replaced by a newer version of the same logical document
WITHDRAWN = "withdrawn"                    # Telegram upload withdrawn by its uploader
REMOVED = "removed"                        # source file deleted from Drive (confirmed)
DUPLICATE = "duplicate"                    # identical content already known; nothing new
CANCELLED = "cancelled"                    # Telegram upload abandoned before activation

PENDING_STATUSES = (AWAITING_NAME, PROCESSING, PENDING_LOCATION, PENDING_REPLACEMENT)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_id INTEGER,
    source TEXT NOT NULL CHECK (source IN ('drive', 'telegram')),
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    alt_norm_title TEXT,
    filename TEXT,
    scope TEXT,
    scope_method TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    version_ts TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    content_hash TEXT,
    text TEXT,
    extraction_method TEXT,
    storage_path TEXT,
    drive_file_id TEXT,
    drive_link TEXT,
    tg_file_id TEXT,
    tg_chat_id INTEGER,
    uploader_id INTEGER,
    uploader_name TEXT,
    superseded_by INTEGER,
    candidate_id INTEGER,
    history TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS ix_documents_drive ON documents(drive_file_id);
CREATE INDEX IF NOT EXISTS ix_documents_uploader ON documents(uploader_id);
CREATE INDEX IF NOT EXISTS ix_documents_logical ON documents(logical_id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    scope TEXT NOT NULL,
    note TEXT,
    embedding BLOB,
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);

CREATE TABLE IF NOT EXISTS drive_files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mime_type TEXT,
    modified_time TEXT,
    md5 TEXT,
    size INTEGER,
    web_link TEXT,
    state TEXT NOT NULL DEFAULT 'present',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    last_processed_modified TEXT,
    last_document_id INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    real_name TEXT,
    name_set_at TEXT,
    state TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    listed INTEGER,
    added INTEGER,
    updated INTEGER,
    unchanged INTEGER,
    removed INTEGER,
    pending INTEGER,
    errors INTEGER,
    message TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_iso(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Document:
    id: int
    logical_id: int | None
    source: str
    title: str
    norm_title: str
    alt_norm_title: str | None
    filename: str | None
    scope: list[str] | None
    scope_method: str | None
    status: str
    status_reason: str | None
    version_ts: str
    ingested_at: str
    content_hash: str | None
    text: str | None
    extraction_method: str | None
    storage_path: str | None
    drive_file_id: str | None
    drive_link: str | None
    tg_file_id: str | None
    tg_chat_id: int | None
    uploader_id: int | None
    uploader_name: str | None
    superseded_by: int | None
    candidate_id: int | None
    history: list[dict] = field(default_factory=list)

    @property
    def version_dt(self) -> datetime:
        return from_iso(self.version_ts)

    @property
    def title_keys(self) -> set[str]:
        return {k for k in (self.norm_title, self.alt_norm_title) if k}

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Document":
        data = dict(row)
        data["scope"] = json.loads(data["scope"]) if data["scope"] else None
        data["history"] = json.loads(data["history"] or "[]")
        return cls(**data)


@dataclass
class Chunk:
    id: int
    document_id: int
    idx: int
    text: str
    scope: list[str]
    note: str | None
    embedding: bytes | None
    embedding_model: str | None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._tx_depth = 0

    def _migrate(self) -> None:
        """Add columns introduced after the first release (existing databases keep their data)."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(drive_files)").fetchall()}
        for name, ddl in (("access_failures", "INTEGER NOT NULL DEFAULT 0"), ("access_retry_at", "TEXT")):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE drive_files ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ transactions
    def transaction(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                if store._tx_depth == 0:
                    store.conn.execute("BEGIN IMMEDIATE")
                store._tx_depth += 1
                return store

            def __exit__(self_inner, exc_type, exc, tb):
                store._tx_depth -= 1
                if store._tx_depth == 0:
                    store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    # ------------------------------------------------------------ documents
    def insert_document(self, **fields: Any) -> Document:
        fields = dict(fields)
        fields.setdefault("ingested_at", to_iso(utcnow()))
        fields.setdefault("history", [])
        if "scope" in fields and fields["scope"] is not None:
            fields["scope"] = json.dumps(fields["scope"])
        fields["history"] = json.dumps(fields["history"], ensure_ascii=False)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", list(fields.values()))
        doc_id = cur.lastrowid
        if fields.get("logical_id") is None:
            self.conn.execute("UPDATE documents SET logical_id = ? WHERE id = ?", (doc_id, doc_id))
        return self.get_document(doc_id)

    def update_document(self, doc_id: int, **fields: Any) -> Document:
        if "scope" in fields and fields["scope"] is not None:
            fields["scope"] = json.dumps(fields["scope"])
        if "history" in fields:
            fields["history"] = json.dumps(fields["history"], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", [*fields.values(), doc_id])
        return self.get_document(doc_id)

    def add_history(self, doc_id: int, event: str, **details: Any) -> None:
        doc = self.get_document(doc_id)
        entry = {"at": to_iso(utcnow()), "event": event, **details}
        self.update_document(doc_id, history=[*doc.history, entry])

    def get_document(self, doc_id: int) -> Document | None:
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return Document.from_row(row) if row else None

    def documents(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[Document]:
        rows = self.conn.execute(f"SELECT * FROM documents WHERE {where} ORDER BY id", tuple(params)).fetchall()
        return [Document.from_row(r) for r in rows]

    def active_documents(self) -> list[Document]:
        return self.documents("status = ?", (ACTIVE,))

    def documents_by_status(self, *statuses: str) -> list[Document]:
        marks = ",".join("?" for _ in statuses)
        return self.documents(f"status IN ({marks})", statuses)

    def documents_for_drive_file(self, file_id: str) -> list[Document]:
        return self.documents("drive_file_id = ?", (file_id,))

    def documents_by_uploader(self, user_id: int) -> list[Document]:
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE source = 'telegram' AND uploader_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
        return [Document.from_row(r) for r in rows]

    def find_by_hash(self, content_hash: str, statuses: tuple[str, ...]) -> list[Document]:
        marks = ",".join("?" for _ in statuses)
        return self.documents(f"content_hash = ? AND status IN ({marks})", (content_hash, *statuses))

    # ------------------------------------------------------------ chunks
    def replace_chunks(self, document_id: int, chunks: list[dict]) -> None:
        self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        for i, c in enumerate(chunks):
            self.conn.execute(
                "INSERT INTO chunks (document_id, idx, text, scope, note, embedding, embedding_model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, i, c["text"], json.dumps(c["scope"]), c.get("note"), c.get("embedding"),
                 c.get("embedding_model")),
            )

    def set_chunk_embedding(self, chunk_id: int, embedding: bytes, model: str) -> None:
        self.conn.execute("UPDATE chunks SET embedding = ?, embedding_model = ? WHERE id = ?",
                          (embedding, model, chunk_id))

    def active_chunks(self) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT c.* FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.status = ? ORDER BY c.id",
            (ACTIVE,),
        ).fetchall()
        return [self._chunk(r) for r in rows]

    def chunks_for(self, document_id: int) -> list[Chunk]:
        rows = self.conn.execute("SELECT * FROM chunks WHERE document_id = ? ORDER BY idx", (document_id,)).fetchall()
        return [self._chunk(r) for r in rows]

    @staticmethod
    def _chunk(row: sqlite3.Row) -> Chunk:
        d = dict(row)
        d["scope"] = json.loads(d["scope"])
        return Chunk(**d)

    # ------------------------------------------------------------ drive files
    def get_drive_file(self, file_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM drive_files WHERE file_id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def drive_files(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self.conn.execute("SELECT * FROM drive_files WHERE state = ?", (state,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM drive_files").fetchall()
        return [dict(r) for r in rows]

    def upsert_drive_file(self, file_id: str, **fields: Any) -> None:
        existing = self.get_drive_file(file_id)
        if existing is None:
            fields.setdefault("first_seen_at", to_iso(utcnow()))
            cols = ", ".join(["file_id", *fields])
            marks = ", ".join("?" for _ in range(len(fields) + 1))
            self.conn.execute(f"INSERT INTO drive_files ({cols}) VALUES ({marks})", [file_id, *fields.values()])
        elif fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE drive_files SET {sets} WHERE file_id = ?", [*fields.values(), file_id])

    # ------------------------------------------------------------ users
    def get_user(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return {"user_id": user_id, "real_name": None, "name_set_at": None, "state": {}}
        d = dict(row)
        d["state"] = json.loads(d["state"] or "{}")
        return d

    def set_user_name(self, user_id: int, name: str) -> None:
        now = to_iso(utcnow())
        self.conn.execute(
            "INSERT INTO users (user_id, real_name, name_set_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET real_name = excluded.real_name, "
            "name_set_at = excluded.name_set_at, updated_at = excluded.updated_at",
            (user_id, name, now, now),
        )

    def get_state(self, user_id: int) -> dict:
        return self.get_user(user_id)["state"]

    def save_state(self, user_id: int, state: dict) -> None:
        now = to_iso(utcnow())
        self.conn.execute(
            "INSERT INTO users (user_id, state, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
            (user_id, json.dumps(state, ensure_ascii=False), now),
        )

    # ------------------------------------------------------------ sync runs
    def start_sync_run(self) -> int:
        cur = self.conn.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (to_iso(utcnow()),))
        return cur.lastrowid

    def finish_sync_run(self, run_id: int, ok: bool, message: str, **counts: int) -> None:
        fields = {"finished_at": to_iso(utcnow()), "ok": int(ok), "message": message, **counts}
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE sync_runs SET {sets} WHERE id = ?", [*fields.values(), run_id])

    def has_successful_sync(self) -> bool:
        return self.conn.execute("SELECT 1 FROM sync_runs WHERE ok = 1 LIMIT 1").fetchone() is not None

    def last_sync_run(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
