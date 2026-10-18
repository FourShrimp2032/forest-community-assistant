"""Shared fixtures. Deterministic: no network, no live credentials, no embedding model."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from forest_bot.conversation import Conversation
from forest_bot.drive import DriveFile, DriveSync
from forest_bot.library import Library
from forest_bot.qa import QAService
from forest_bot.retrieval import Retriever
from forest_bot.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"
KYIV = ZoneInfo("Europe/Kyiv")
ARCHIVE_TS = "2026-10-13T07:30:00+00:00"


def archive_texts() -> list[dict]:
    return json.loads((FIXTURES / "archive_texts.json").read_text("utf-8"))


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class FakeLLM:
    """Records prompts; router returns `route` (or {}), answers are produced by `answer_fn`."""

    def __init__(self, route: dict | None = None, answer_fn=None, scope_fn=None):
        self.route = route
        self.answer_fn = answer_fn or (lambda messages: {"status": "answered",
                                                         "answer": "Відповідь із документа [1].", "used": [1]})
        self.scope_fn = scope_fn
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, model, messages, **kwargs):
        r = await self.chat_json(model, messages, **kwargs)
        return f"НОТАТКИ:\n[1] так — тест\nСТАТУС: {r.get('status', 'answered')}\nВІДПОВІДЬ:\n{r.get('answer', '')}"

    async def chat_json(self, model, messages, **kwargs):
        self.calls.append((model, messages))
        system = messages[0]["content"]
        if system.startswith("You route"):
            route = self.route(messages) if callable(self.route) else self.route
            return dict(route) if route else {}
        if system.startswith("You classify"):
            return self.scope_fn(messages) if self.scope_fn else {"codes": [], "confident": False}
        return self.answer_fn(messages)

    @property
    def answer_prompts(self) -> list[str]:
        return [m[1]["content"] for _, m in self.calls if m[0]["content"].startswith("Ти — помічник")]


def evidence_titles(prompt: str) -> list[str]:
    return re.findall(r"^\[\d+\] «(.+?)»$", prompt, re.M)


class FakeDriveClient:
    """In-memory Drive folder. Set `fail_listing` to simulate an API outage."""

    def __init__(self):
        self.files: dict[str, tuple[DriveFile, bytes]] = {}
        self.fail_listing = False
        self.fail_download: set[str] = set()
        self.incomplete_listings = 0        # next N listings omit `hidden` files (simulated partial results)
        self.hidden: set[str] = set()
        self.listings = 0
        self.downloads = 0

    def put(self, file_id: str, name: str, content: str | bytes, modified: str, mime: str = "text/plain"):
        data = content.encode("utf-8") if isinstance(content, str) else content
        f = DriveFile(file_id, name, mime, ts(modified).isoformat(timespec="microseconds"), md5=str(hash(data)),
                      size=len(data), web_link=f"https://drive.google.com/file/d/{file_id}/view")
        self.files[file_id] = (f, data)

    def delete(self, file_id: str):
        self.files.pop(file_id)

    async def list_folder(self, folder_id):
        self.listings += 1
        if self.fail_listing:
            raise RuntimeError("HTTP 503 backend error")
        if self.incomplete_listings:
            self.incomplete_listings -= 1
            return [f for f, _ in self.files.values() if f.id not in self.hidden]
        return [f for f, _ in self.files.values()]

    async def download(self, f):
        if f.id in self.fail_download:
            raise RuntimeError("download timeout")
        self.downloads += 1
        return self.files[f.id][1]


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def library(store, tmp_path):
    return Library(store, tmp_path / "files")


@pytest.fixture
def drive():
    return FakeDriveClient()


@pytest.fixture
def sync(drive, store, library):
    return DriveSync(drive, store, library, "folder")


def load_archive_into(drive: FakeDriveClient, modified: str = ARCHIVE_TS) -> dict[str, str]:
    """Put the real archive texts (fixture) into the fake Drive. Returns title -> file id."""
    ids = {}
    for i, d in enumerate(archive_texts()):
        stem = d["filename"].rsplit(".", 1)[0]
        file_id = f"file{i:03d}"
        drive.put(file_id, stem + ".txt", d["text"], modified)
        ids[stem] = file_id
    return ids


@pytest.fixture
async def archive(drive, sync):
    ids = load_archive_into(drive)
    report = await sync.run()
    assert report.ok and report.added == len(ids)
    return ids


def make_conversation(store, library, llm) -> Conversation:
    retriever = Retriever(store, None, max_docs=8, per_target_docs=4, evidence_chars=9000)
    qa = QAService(llm, retriever, model="answer-model", router_model="router-model", tz=KYIV)
    conv = Conversation(store, library, qa, KYIV, max_upload_mb=20, page_size=3)
    return conv


@pytest.fixture
def fake_files():
    """Telegram file storage for the fake downloader."""
    return {}


@pytest.fixture
def conversation(store, library, fake_files):
    llm = FakeLLM()
    conv = make_conversation(store, library, llm)
    conv.llm = llm

    async def downloader(file_id):
        return fake_files[file_id]

    conv.downloader = downloader
    return conv
