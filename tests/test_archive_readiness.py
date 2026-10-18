"""Initial-archive readiness: notices while loading, and rejected vs. temporarily waiting files."""

from forest_bot import messages as msg
from forest_bot import storage as st
from forest_bot.drive import DrivePermanentError, DriveThrottled, archive_status

from .conftest import load_archive_into

QUESTION_FOUND = "Скільки внесок під дубом і до якого числа?"
QUESTION_MISSING = "Який пароль від Wi-Fi під дубом?"
ROUTE = {"intent": "question", "locations": ["pid_dubom"], "needs_location": False,
         "search_query": "Членський внесок", "keywords": ["внесок", "числа"]}


def _block_after(drive, n):
    """Downloads succeed for the first n files, then Google's block."""
    real = drive.download
    count = {"n": 0}

    async def download(f):
        count["n"] += 1
        if count["n"] > n:
            raise DriveThrottled("automated queries")
        return await real(f)

    drive.download = download
    return real


# ------------------------------------------------------------------ status from stored state

async def test_status_before_first_listing_and_when_sync_is_disabled(store):
    assert archive_status(store, sync_enabled=False).loading is False
    fresh = archive_status(store, sync_enabled=True)
    assert fresh.loading and not fresh.listed_once                      # nothing listed yet: loading


async def test_status_counts_only_never_processed_files_as_waiting(store, drive, sync):
    load_archive_into(drive)
    real = _block_after(drive, 41)
    await sync.run()
    status = archive_status(store, True)
    assert status.loading and (status.waiting, status.total) == (37, 78)
    drive.download = real
    await sync.run()
    assert not archive_status(store, True).loading


async def test_routine_sync_of_a_changed_file_is_not_loading(archive, store, drive, sync):
    fid = next(iter(drive.files))
    f, data = drive.files[fid]
    drive.put(fid, f.name, data.decode() + "\nДоповнення.", "2026-10-20T10:00:00Z")
    _block_after(drive, 0)                                          # the changed file cannot be downloaded
    await sync.run()
    assert not archive_status(store, True).loading                  # previous version still active


async def test_rejected_files_are_not_counted_as_loading(store, drive, sync, library):
    drive.put("ok", "Календар.txt", "Календар\nЦей календар для всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    drive.put("img", "фото.jpg", b"\xff\xd8\xff", "2026-10-01T10:00:00Z", mime="image/jpeg")
    drive.put("big", "Великий.pdf", b"%PDF-" + b"x" * 300, "2026-10-01T10:00:00Z", mime="application/pdf")
    drive.max_download_bytes = 200
    await sync.run()
    status = archive_status(store, True)
    assert not status.loading and status.waiting == 0
    reasons = {d.title: d.status_reason for d in library.drive_pending()}
    assert "не підтримується" in reasons["фото"] and "завеликий" in reasons["Великий"]


# ------------------------------------------------------------------ oversized and permanent errors

async def test_oversized_file_is_rejected_visibly_and_not_retried_until_changed(store, drive, sync, library):
    drive.max_download_bytes = 200
    drive.put("big", "Великий.pdf", b"%PDF-" + b"x" * 300, "2026-10-01T10:00:00Z", mime="application/pdf")
    report = await sync.run()
    assert report.pending == 1 and drive.downloads == 0                 # not even downloaded
    doc = library.drive_pending()[0]
    assert doc.status == st.FAILED and "ліміт" in doc.status_reason
    again = await sync.run()
    assert again.unchanged == 1 and again.errors == 0                   # not retried every sync
    drive.put("big", "Великий.txt", "Великий\nЦя пам'ятка для всіх мешканців лісу.", "2026-10-02T10:00:00Z")
    await sync.run()                                                    # changed (now small) -> processed
    assert library.drive_pending() == [] and len(store.active_documents()) == 1


async def test_permanent_download_error_is_rejected_but_transient_error_waits(store, drive, sync, library):
    drive.put("gone", "Зниклий.txt", "Зниклий\nДля всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    drive.put("flaky", "Нестабільний.txt", "Нестабільний\nДля всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    real = drive.download

    async def download(f):
        if f.id == "gone":
            raise DrivePermanentError("HTTP 404: not found")
        if f.id == "flaky":
            raise RuntimeError("HTTP 503 after retries")
        return await real(f)

    drive.download = download
    report = await sync.run()
    assert report.pending == 1 and report.errors == 1
    assert [d.title for d in library.drive_pending()] == ["Зниклий"]
    status = archive_status(store, True)
    assert status.loading and status.waiting == 1                       # only the transient one is waiting


# ------------------------------------------------------------------ user-facing notices

async def _partial_archive(drive, sync, n=41):
    load_archive_into(drive)
    _block_after(drive, n)
    await sync.run()


async def test_answer_during_loading_carries_a_short_notice(conversation, drive, sync):
    conversation.archive_sync_enabled = True
    await _partial_archive(drive, sync, n=78 - 5)          # the fee document is among the processed ones
    conversation.llm.route = ROUTE
    html = (await conversation.on_text(1, QUESTION_FOUND))[-1].text
    assert html.startswith("⏳ «Лісовий архів» ще завантажується: 5 із 78 файлів поки недоступні.")
    assert "Джерела" in html


async def test_not_found_during_loading_does_not_claim_the_whole_archive_was_searched(conversation, drive, sync):
    conversation.archive_sync_enabled = True
    await _partial_archive(drive, sync)
    conversation.llm.route = ROUTE
    conversation.llm.answer_fn = lambda m: {"status": "not_found", "answer": "У документах цього немає."}
    html = (await conversation.on_text(1, QUESTION_MISSING))[-1].text
    assert html == msg.NOT_FOUND_WHILE_LOADING.format(waiting=37, total=78)
    assert "У документах цього немає" not in html


async def test_not_found_before_the_first_listing_uses_the_unknown_total_wording(conversation):
    conversation.archive_sync_enabled = True
    html = (await conversation.on_text(1, QUESTION_MISSING))[-1].text
    assert html == msg.NOT_FOUND_WHILE_LOADING_UNKNOWN


async def test_no_notice_once_the_archive_is_complete(conversation, archive):
    conversation.archive_sync_enabled = True
    conversation.llm.route = ROUTE
    html = (await conversation.on_text(1, QUESTION_FOUND))[-1].text
    assert "ще завантажується" not in html
    conversation.llm.answer_fn = lambda m: {"status": "not_found", "answer": "Такої інформації не знайдено."}
    html = (await conversation.on_text(2, QUESTION_MISSING))[-1].text
    assert "ще завантажується" not in html


# ------------------------------------------------------------------ 403: block vs rate limit vs access

GOOGLE_SORRY_HTML = ("<html><head><title>Sorry...</title></head><body><div>We're sorry...</div><div>... but your "
                     "computer or network may be sending automated queries. To protect our users, we can't process "
                     "your request right now.</div></body></html>")


def _client(handler):
    import httpx
    from forest_bot.drive import DriveClient
    client = DriveClient("key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_html_anti_automation_403_is_a_block_not_a_rejection():
    import httpx
    import pytest
    client = _client(lambda r: httpx.Response(403, text=GOOGLE_SORRY_HTML,
                                              headers={"content-type": "text/html; charset=UTF-8"}))
    with pytest.raises(DriveThrottled):
        await client._get("https://www.googleapis.com/drive/v3/files/x", {"alt": "media"})


async def test_drive_rate_limit_403_is_temporary_and_access_403_is_rejection():
    import httpx
    import pytest

    def json403(reason):
        return lambda r: httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": reason}]}})

    with pytest.raises(DriveThrottled):
        await _client(json403("userRateLimitExceeded"))._get("https://x/files/a", {})
    with pytest.raises(DrivePermanentError) as exc:
        await _client(json403("insufficientFilePermissions"))._get("https://x/files/a", {})
    assert exc.value.kind == "access" and "insufficientFilePermissions" in str(exc.value)


async def test_anti_automation_block_keeps_the_file_waiting_in_sync(store, drive, sync, library):
    drive.put("f1", "Календар.txt", "Календар\nДля всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    real_client = _client(lambda r: __import__("httpx").Response(
        403, text=GOOGLE_SORRY_HTML, headers={"content-type": "text/html"}))

    async def download(f):                       # the real DriveClient code path with the observed response
        return await real_client.download(f)

    drive.download = download
    report = await sync.run()
    assert sync.throttled and report.pending == 0
    assert library.drive_pending() == []                          # not rejected
    assert archive_status(store, True).waiting == 1               # still waiting for download


# ------------------------------------------------------------------ access errors: bounded recovery

def test_access_recheck_schedule_is_bounded():
    from forest_bot.drive import access_recheck_delay
    assert [access_recheck_delay(n) for n in (1, 2, 3, 4, 5, 6, 7)] == [3600, 7200, 14400, 28800, 57600,
                                                                         86400, 86400]


def _due_now(store, file_id):
    """Simulate that the recheck time has passed."""
    store.upsert_drive_file(file_id, access_retry_at="2000-01-01T00:00:00+00:00")


async def test_access_error_rejects_visibly_keeps_previous_version_and_recovers_without_file_change(
        archive, store, drive, sync, library):
    fid = archive["Календар сезонів"]
    previous = next(d for d in store.active_documents() if d.title == "Календар сезонів")
    f, data = drive.files[fid]
    drive.put(fid, f.name, data, "2026-10-20T10:00:00Z")          # touched, content unchanged
    real = drive.download
    denied = {"on": True}

    async def download(file):
        if file.id == fid and denied["on"]:
            raise DrivePermanentError("HTTP 403: insufficientFilePermissions", kind="access")
        return await real(file)

    drive.download = download
    await sync.run()
    pending = library.drive_pending()
    assert [d.title for d in pending] == ["Календар сезонів"]
    assert "перевірка повториться автоматично" in pending[0].status_reason
    assert store.get_document(previous.id).status == st.ACTIVE     # previous valid version preserved
    assert not archive_status(store, True).loading                  # rejected, not "loading"
    downloads = drive.downloads

    await sync.run()                                                # before the recheck time: no attempt
    assert drive.downloads == downloads

    _due_now(store, fid)                                            # still no access at recheck
    await sync.run()
    row = store.get_drive_file(fid)
    assert row["access_failures"] == 2 and row["access_retry_at"] > "2026"
    assert len(store.documents_for_drive_file(fid)) == 2            # no new record per failed recheck

    denied["on"] = False                                            # access restored, file unchanged
    _due_now(store, fid)
    await sync.run()
    assert library.drive_pending() == []
    assert store.get_document(previous.id).status == st.ACTIVE      # same content -> still the active one
    row = store.get_drive_file(fid)
    assert row["access_failures"] == 0 and row["access_retry_at"] is None


async def test_oversized_files_wait_for_a_change_not_for_an_access_recheck(store, drive, sync):
    drive.max_download_bytes = 200
    drive.put("big", "Великий.pdf", b"%PDF-" + b"x" * 300, "2026-10-01T10:00:00Z", mime="application/pdf")
    await sync.run()
    assert store.get_drive_file("big")["access_retry_at"] is None


def test_existing_database_is_migrated_in_place(tmp_path):
    import sqlite3
    from forest_bot.storage import Store
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE drive_files (file_id TEXT PRIMARY KEY, name TEXT NOT NULL, mime_type TEXT, "
                 "modified_time TEXT, md5 TEXT, size INTEGER, web_link TEXT, state TEXT NOT NULL DEFAULT 'present', "
                 "first_seen_at TEXT NOT NULL, last_seen_at TEXT, last_processed_modified TEXT, "
                 "last_document_id INTEGER, last_error TEXT)")
    conn.execute("INSERT INTO drive_files (file_id, name, first_seen_at) VALUES ('a', 'x', '2026-01-01')")
    conn.commit()
    conn.close()
    store = Store(path)
    row = store.get_drive_file("a")
    assert row["name"] == "x" and row["access_failures"] == 0 and row["access_retry_at"] is None


async def test_generic_html_error_page_is_not_mistaken_for_the_anti_automation_block():
    import httpx
    import pytest
    page = "<html><body>Sorry, the file you have requested does not exist.</body></html>"
    client = _client(lambda r: httpx.Response(404, text=page, headers={"content-type": "text/html"}))
    with pytest.raises(DrivePermanentError) as exc:
        await client._get("https://www.googleapis.com/drive/v3/files/x", {"alt": "media"})
    assert exc.value.kind == "access"                       # visible in /pending and rechecked, not "waiting"
