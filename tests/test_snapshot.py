"""Bundled bootstrap snapshot: guarded import, no repeated OCR, Drive sync on top, restarts, rebuild."""

import hashlib
import json
import shutil

import pytest

from forest_bot import messages as msg
from forest_bot import storage as st
from forest_bot.drive import DriveFile, DriveSync, archive_status
from forest_bot.library import Library
from forest_bot.snapshot import bootstrap, bootstrap_info, build_snapshot, load_manifest
from forest_bot.storage import Store

from .conftest import SNAPSHOT

ORIGINAL = "1b_hEjnkQfP8ZVP-OEbnYWqoGSFwXMAP3"
MANIFEST = load_manifest(SNAPSHOT)
QUESTION = "Скільки внесок під дубом і до якого числа?"
ROUTE = {"intent": "question", "locations": ["pid_dubom"], "needs_location": False,
         "search_query": "Членський внесок", "keywords": ["внесок", "числа"]}


def no_extraction(library):
    """Fail loudly if anything tries to extract/OCR during the import."""
    calls = []

    async def extract(data, name):
        calls.append(name)
        raise AssertionError(f"extraction attempted for {name}")

    library.extract = extract
    return calls


def drive_like_snapshot(drive):
    """The fake Drive folder holds exactly the snapshot's files (same IDs, modifiedTime, md5)."""
    for e in MANIFEST["files"]:
        data = (SNAPSHOT / e["file"]).read_bytes()
        drive.files[e["id"]] = (DriveFile(e["id"], e["name"], e["mime_type"], e["modified_time"], md5=e["md5"],
                                          size=e["size"], web_link=e["web_link"]), data)


def statuses(store):
    return {d.id: d.status for d in store.documents("1 = 1")}


# ------------------------------------------------------------------ manifest content

def test_manifest_has_provenance_and_only_the_original_archive():
    assert MANIFEST["folder_id"] == ORIGINAL and MANIFEST["file_count"] == len(MANIFEST["files"]) == 78
    assert MANIFEST["created_at"]
    for e in MANIFEST["files"]:
        assert e["id"] and e["name"] and e["mime_type"] and e["modified_time"] and e["md5"] and e["web_link"]
        data = (SNAPSHOT / e["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == e["sha256"] and hashlib.md5(data).hexdigest() == e["md5"]
        assert e["text"] and e["extraction_method"]
    assert {p.name for p in (SNAPSHOT / "files").iterdir()} == {e["file"].split("/")[1] for e in MANIFEST["files"]}
    assert not list(SNAPSHOT.rglob("*.sqlite3*"))


# ------------------------------------------------------------------ fresh start

async def test_fresh_start_imports_locally_without_drive_ocr_or_llm(store, library):
    extractions = no_extraction(library)

    async def classifier(title, text):
        raise AssertionError("LLM scope classification attempted")

    library.scope_classifier = classifier
    result = await bootstrap(store, library, SNAPSHOT, ORIGINAL)
    assert result.imported == 78 and extractions == []
    active = store.active_documents()
    assert len(active) == 78 and all(d.source == "drive" for d in active)
    by_id = {d.drive_file_id: d for d in active}
    for e in MANIFEST["files"]:
        d = by_id[e["id"]]
        assert d.version_ts == e["modified_time"] and d.drive_link == e["web_link"] and d.text == e["text"]
    status = archive_status(store, sync_enabled=True)
    assert status.snapshot_unverified and not status.loading


async def test_google_outage_keeps_the_snapshot_usable_and_says_freshness_is_unverified(
        conversation, store, library, drive):
    await bootstrap(store, library, SNAPSHOT, ORIGINAL)
    drive.fail_listing = True
    report = await DriveSync(drive, store, library, ORIGINAL).run()
    assert not report.ok and len(store.active_documents()) == 78
    conversation.archive_sync_enabled = True
    conversation.llm.route = ROUTE
    html = (await conversation.on_text(1, QUESTION))[-1].text
    date = MANIFEST["created_at"][8:10] + "." + MANIFEST["created_at"][5:7] + "." + MANIFEST["created_at"][:4]
    assert html.startswith(msg.SNAPSHOT_UNVERIFIED.format(date=date)) and "Джерела" in html
    assert "завантажується" not in html

    conversation.llm.answer_fn = lambda m: {"status": "not_found", "answer": "Немає."}
    html = (await conversation.on_text(2, "Який пароль від Wi-Fi під дубом?"))[-1].text
    assert html == msg.NOT_FOUND_SNAPSHOT_UNVERIFIED.format(date=date)


async def test_without_a_snapshot_an_empty_database_is_still_loading(store, library, tmp_path):
    result = await bootstrap(store, library, tmp_path / "missing", ORIGINAL)
    assert result.skipped == "no snapshot" and archive_status(store, True).loading


# ------------------------------------------------------------------ sync on top of the snapshot

async def test_unchanged_files_are_not_downloaded_and_freshness_is_then_verified(conversation, store, library,
                                                                                  drive):
    await bootstrap(store, library, SNAPSHOT, ORIGINAL)
    drive_like_snapshot(drive)
    report = await DriveSync(drive, store, library, ORIGINAL).run()
    assert report.ok and (report.unchanged, report.added, report.updated) == (78, 0, 0) and drive.downloads == 0
    status = archive_status(store, True)
    assert not status.snapshot_unverified and not status.loading
    conversation.archive_sync_enabled = True
    conversation.llm.route = ROUTE
    html = (await conversation.on_text(1, QUESTION))[-1].text
    assert not html.startswith("ℹ️") and not html.startswith("⏳")


async def test_remote_update_and_confirmed_deletion_apply_to_snapshot_records(store, library, drive):
    await bootstrap(store, library, SNAPSHOT, ORIGINAL)
    drive_like_snapshot(drive)
    changed = next(e for e in MANIFEST["files"] if e["name"].endswith(".txt"))
    gone = next(e for e in MANIFEST["files"] if e["name"].endswith(".txt") and e["id"] != changed["id"])
    f, data = drive.files[changed["id"]]
    new_data = data + "\nДоповнення від правління.".encode()
    drive.files[changed["id"]] = (DriveFile(f.id, f.name, f.mime_type, "2026-11-01T09:00:00.000000+00:00",
                                            md5=hashlib.md5(new_data).hexdigest(), size=len(new_data),
                                            web_link=f.web_link), new_data)
    del drive.files[gone["id"]]
    report = await DriveSync(drive, store, library, ORIGINAL).run()
    assert report.ok and drive.downloads == 1 and report.updated == 1 and report.removed == 1
    versions = store.documents_for_drive_file(changed["id"])
    assert [d.status for d in versions] == [st.SUPERSEDED, st.ACTIVE]
    assert versions[-1].version_ts.startswith("2026-11-01") and "Доповнення від правління" in versions[-1].text
    assert all(d.status == st.REMOVED for d in store.documents_for_drive_file(gone["id"]))
    assert len(store.active_documents()) == 77


async def test_a_corrupted_bundled_file_is_skipped_and_left_for_drive_sync(store, library, drive, tmp_path):
    snap = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT, snap)
    victim = MANIFEST["files"][0]
    (snap / victim["file"]).write_bytes(b"corrupted")
    assert (await bootstrap(store, library, snap, ORIGINAL)).imported == 77
    assert archive_status(store, True).snapshot_unverified
    drive_like_snapshot(drive)
    await DriveSync(drive, store, library, ORIGINAL).run()
    assert drive.downloads == 1 and len(store.active_documents()) == 78


# ------------------------------------------------------------------ isolation and idempotency

async def test_custom_or_test_folders_are_never_seeded_with_the_snapshot(store, library, drive):
    assert (await bootstrap(store, library, SNAPSHOT, "some-test-folder")).skipped == "different folder"
    assert store.count_documents() == 0 and store.drive_files() == [] and bootstrap_info(store) is None
    drive.put("t1", "Календар.txt", "Календар\nЦей календар для всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    await DriveSync(drive, store, library, "some-test-folder").run()
    assert [d.title for d in store.active_documents()] == ["Календар"]       # only the folder's own files


async def test_existing_installations_keep_their_state(store, library, drive):
    drive.put("own", "Календар.txt", "Календар\nЦей календар для всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    await DriveSync(drive, store, library, ORIGINAL).run()
    before = statuses(store)
    assert (await bootstrap(store, library, SNAPSHOT, ORIGINAL)).skipped == "database not empty"
    assert statuses(store) == before and bootstrap_info(store) is None


async def test_restart_never_reimports_or_restores_superseded_withdrawn_or_removed(tmp_path, drive):
    db = tmp_path / "forest.sqlite3"
    store = Store(db)
    library = Library(store, tmp_path / "files")
    await bootstrap(store, library, SNAPSHOT, ORIGINAL)
    drive_like_snapshot(drive)
    changed, gone = [e for e in MANIFEST["files"] if e["name"].endswith(".txt")][:2]
    f, data = drive.files[changed["id"]]
    drive.files[changed["id"]] = (DriveFile(f.id, f.name, f.mime_type, "2026-11-01T09:00:00.000000+00:00",
                                            md5="new", size=1, web_link=f.web_link),
                                  data + "\nНова редакція.".encode())
    del drive.files[gone["id"]]
    await DriveSync(drive, store, library, ORIGINAL).run()
    tg = store.insert_document(source="telegram", title="Ліхтар", norm_title="ліхтар", filename="Ліхтар.txt",
                               status=st.WITHDRAWN, version_ts="2026-10-18T09:00:00+00:00")
    before = statuses(store)
    assert before[tg.id] == st.WITHDRAWN and st.SUPERSEDED in before.values() and st.REMOVED in before.values()
    store.close()

    for _ in range(2):                                   # two restarts
        store = Store(db)
        library = Library(store, tmp_path / "files")
        assert (await bootstrap(store, library, SNAPSHOT, ORIGINAL)).skipped == "already bootstrapped"
        assert statuses(store) == before
        store.close()


def test_unsupported_snapshot_format_is_not_imported(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"format_version": 99}), "utf-8")
    with pytest.raises(Exception):
        load_manifest(tmp_path)


# ------------------------------------------------------------------ rebuilding the snapshot

class Md5Drive:
    """Fake Drive client with real md5Checksum values (as the Drive API reports them)."""

    def __init__(self, files):
        self.files = files
        self.downloads = 0

    async def list_folder(self, folder_id):
        return [DriveFile(i, n, "text/plain", m, md5=hashlib.md5(d).hexdigest(), size=len(d),
                          web_link=f"https://drive.google.com/file/d/{i}/view")
                for i, (n, d, m) in self.files.items()]

    async def download(self, f):
        self.downloads += 1
        return self.files[f.id][1]


async def test_snapshot_build_is_reproducible_and_reuses_verified_files(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "Внески.txt").write_bytes("Внески під дубом: 4 жолуді.".encode())
    (seed / "Правила.txt").write_bytes("СТАРА версія, не та, що на Drive.".encode())
    client = Md5Drive({"a": ("Внески.txt", "Внески під дубом: 4 жолуді.".encode(), "2026-10-01T10:00:00+00:00"),
                       "b": ("Правила.txt", "Правила для всього лісу.".encode(), "2026-10-02T10:00:00+00:00")})
    out = tmp_path / "snap"
    m1 = await build_snapshot(client, "folder", out, [seed], download_delay=0)
    assert m1["_stats"] == {"reused": 0, "seeded": 1, "downloaded": 1, "extracted": 2}   # md5 mismatch -> download
    m2 = await build_snapshot(client, "folder", out, [seed], download_delay=0)
    assert m2["_stats"] == {"reused": 2, "seeded": 0, "downloaded": 0, "extracted": 0} and client.downloads == 1
    strip = lambda m: [{k: v for k, v in e.items()} for e in m["files"]]               # noqa: E731
    assert strip(m1) == strip(m2)
    assert [e["text"] for e in m2["files"]] == ["Внески під дубом: 4 жолуді.", "Правила для всього лісу."]

    client.files["b"] = ("Правила.txt", "Нові правила.".encode(), "2026-10-05T10:00:00+00:00")
    del client.files["a"]
    m3 = await build_snapshot(client, "folder", out, download_delay=0)
    assert m3["_stats"]["downloaded"] == 1 and m3["_stats"]["extracted"] == 1
    assert [e["id"] for e in m3["files"]] == ["b"] and sorted(p.name for p in (out / "files").iterdir()) == ["b.txt"]
