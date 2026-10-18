"""Live Google Drive checks (read-only). Run:  pytest -m live tests/live/test_live_drive.py

Requires GOOGLE_DRIVE_API_KEY. Uses GOOGLE_DRIVE_FOLDER_ID (default: the original «Лісовий архів»).
"""

import os
import tempfile

import pytest

from forest_bot.drive import DriveClient, DriveSync
from forest_bot.extraction import extract_text
from forest_bot.library import Library
from forest_bot.storage import Store

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("GOOGLE_DRIVE_API_KEY"), reason="GOOGLE_DRIVE_API_KEY not set")]

FOLDER = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "1b_hEjnkQfP8ZVP-OEbnYWqoGSFwXMAP3")


@pytest.fixture
async def client():
    c = DriveClient(os.environ["GOOGLE_DRIVE_API_KEY"], page_size=10)   # small pages: exercises pagination
    yield c
    await c.close()


async def test_listing_all_pages_with_modification_times(client):
    files = await client.list_folder(FOLDER)
    print(f"listed {len(files)} files")
    assert len(files) >= 78 if FOLDER.startswith("1b_hEj") else len(files) > 0
    assert len({f.id for f in files}) == len(files)
    assert all(f.modified_time and f.name for f in files)


async def test_download_and_extract_each_format(client):
    files = await client.list_folder(FOLDER)
    by_ext = {}
    for f in files:
        by_ext.setdefault(f.name.rsplit(".", 1)[-1].lower(), f)
    for ext, f in sorted(by_ext.items()):
        data = await client.download(f)
        text = extract_text(data, f.name).text
        print(f"{ext}: «{f.name}» {len(data)} bytes -> {len(text)} chars")
        assert len(text) > 50


async def test_full_sync_into_temporary_store(client):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(f"{tmp}/db.sqlite3")
        library = Library(store, f"{tmp}/files")
        report = await DriveSync(client, store, library, FOLDER).run()
        print(report)
        assert report.ok and report.errors == 0
        again = await DriveSync(client, store, library, FOLDER).run()
        assert again.ok and again.added == again.updated == again.removed == 0
        store.close()
