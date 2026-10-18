"""Google Drive synchronisation against an in-memory fake folder."""

from forest_bot import storage as st


FEES_DUB = ("Членські внески рукавички «Під дубом»\nВнесок — 4 жолуді з дорослого звіра на місяць, до 10 числа.")


async def test_initial_sync_ingests_whole_archive(archive, store, library):
    assert len(store.active_documents()) == 78
    assert library.drive_pending() == []
    doc = next(d for d in store.active_documents() if d.title == "Членські внески рукавички Під дубом")
    assert doc.scope == ["pid_dubom"] and doc.source == "drive"
    assert doc.version_ts.startswith("2026-10-13T07:30:00")      # Drive modifiedTime, not ingest time
    assert doc.drive_link == f"https://drive.google.com/file/d/{archive['Членські внески рукавички Під дубом']}/view"


async def test_repeated_sync_of_unchanged_files_is_idempotent(archive, sync, drive, store):
    before = [(d.id, d.version_ts) for d in store.active_documents()]
    downloads = drive.downloads
    report = await sync.run()
    assert report.ok and report.unchanged == 78 and report.added == report.updated == 0
    assert drive.downloads == downloads                            # nothing re-downloaded
    assert [(d.id, d.version_ts) for d in store.active_documents()] == before


async def test_touched_file_with_same_content_creates_no_new_version(archive, sync, drive, store):
    fid = archive["Календар сезонів"]
    f, data = drive.files[fid]
    drive.put(fid, f.name, data, "2026-10-20T10:00:00Z")
    report = await sync.run()
    assert report.ok and report.updated == 0
    cal = [d for d in store.active_documents() if d.title == "Календар сезонів"]
    assert len(cal) == 1 and cal[0].version_ts.startswith("2026-10-13")


async def test_updated_file_replaces_previous_version(archive, sync, drive, store):
    fid = archive["Календар сезонів"]
    f, data = drive.files[fid]
    drive.put(fid, f.name, data.decode().replace("у листопаді", "у жовтні"), "2026-10-20T10:00:00Z")
    report = await sync.run()
    assert report.ok and report.updated == 1
    versions = store.documents_for_drive_file(fid)
    assert [v.status for v in versions] == [st.SUPERSEDED, st.ACTIVE]
    assert "у жовтні" in versions[-1].text


async def test_new_file_is_added(archive, sync, drive, store):
    drive.put("new1", "Правила зимівлі.txt", "Правила зимівлі\nЦя пам'ятка для всіх мешканців лісу. Спати тихо.",
              "2026-10-21T10:00:00Z")
    report = await sync.run()
    assert report.added == 1 and len(store.active_documents()) == 79


async def test_confirmed_deletion_stops_use_without_restoring(archive, sync, drive, store, library):
    fid = archive["Календар сезонів"]
    f, data = drive.files[fid]
    drive.put(fid, f.name, data.decode().replace("у листопаді", "у жовтні"), "2026-10-20T10:00:00Z")
    await sync.run()
    drive.delete(fid)
    report = await sync.run()
    assert report.ok and report.removed == 1
    statuses = [v.status for v in store.documents_for_drive_file(fid)]
    assert statuses == [st.SUPERSEDED, st.REMOVED]                 # old version NOT reactivated
    assert not any(d.title == "Календар сезонів" for d in store.active_documents())


async def test_listing_failure_is_not_deletion(archive, sync, drive, store):
    drive.fail_listing = True
    report = await sync.run()
    assert not report.ok and report.removed == 0
    assert len(store.active_documents()) == 78
    assert store.last_sync_run()["ok"] == 0


async def test_confirmed_mass_deletion_is_applied_regardless_of_count(archive, sync, drive, store):
    """60 of 78 files really deleted: confirmed by a second listing and removed, not kept forever."""
    for fid in list(drive.files)[:60]:
        drive.delete(fid)
    listings = drive.listings
    report = await sync.run()
    assert report.ok and report.removed == 60 and "підтверджено" in report.message
    assert drive.listings == listings + 2                         # one extra confirmation listing
    assert len(store.active_documents()) == 18


async def test_incomplete_first_listing_does_not_delete(archive, sync, drive, store):
    """The first listing omits 60 files that still exist; the confirmation listing sees them."""
    drive.hidden = set(list(drive.files)[:60])
    drive.incomplete_listings = 1
    report = await sync.run()
    assert report.removed == 0 and "залишено" in report.message
    assert len(store.active_documents()) == 78


async def test_unconfirmed_mass_deletion_is_retried_and_eventually_applied(archive, sync, drive, store):
    for fid in list(drive.files)[:60]:
        drive.delete(fid)
    real_list = drive.list_folder
    calls = {"n": 0}

    async def first_ok_then_fail(folder_id):
        calls["n"] += 1
        if calls["n"] == 2:                                        # the confirmation listing fails
            raise RuntimeError("HTTP 503")
        return await real_list(folder_id)

    drive.list_folder = first_ok_then_fail
    report = await sync.run()
    assert report.removed == 0 and "відкладено" in report.message
    assert len(store.active_documents()) == 78                    # nothing removed without confirmation
    drive.list_folder = real_list
    report = await sync.run()                                     # next sync confirms and applies
    assert report.removed == 60 and len(store.active_documents()) == 18


async def test_small_confirmed_removal_needs_no_extra_listing(archive, sync, drive, store):
    listings = drive.listings
    drive.delete(next(iter(drive.files)))
    report = await sync.run()
    assert report.removed == 1 and drive.listings == listings + 1


async def test_download_failure_keeps_previous_version_and_retries(archive, sync, drive, store):
    fid = archive["Календар сезонів"]
    f, data = drive.files[fid]
    drive.put(fid, f.name, data.decode().replace("у листопаді", "у жовтні"), "2026-10-20T10:00:00Z")
    drive.fail_download.add(fid)
    report = await sync.run()
    assert report.errors == 1
    assert "у листопаді" in next(d for d in store.active_documents() if d.title == "Календар сезонів").text
    drive.fail_download.clear()
    report = await sync.run()
    assert report.updated == 1
    assert "у жовтні" in next(d for d in store.active_documents() if d.title == "Календар сезонів").text


async def test_ambiguous_location_goes_to_pending_and_activates_after_fix(sync, drive, store, library):
    drive.put("a", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    await sync.run()
    # The file is edited so that its place can no longer be determined (title + content).
    drive.files.pop("a")
    drive.put("a", "Внески.txt", "Внески\nВнесок — 5 жолудів на місяць, до 12 числа.", "2026-10-05T10:00:00Z")
    report = await sync.run()
    assert report.pending == 1
    pending = library.drive_pending()
    assert [p.title for p in pending] == ["Внески"] and pending[0].status == st.PENDING_LOCATION
    # Previous valid version stays active; the ambiguous candidate is not used.
    assert [d.title for d in store.active_documents()] == ["Членські внески рукавички Під дубом"]
    # Fixed in the archive -> next sync reprocesses and activates it as the new version.
    drive.put("a", "Внески.txt", "Внески\nДля мешканців рукавички «Під дубом»: внесок — 5 жолудів, до 12 числа.",
              "2026-10-07T10:00:00Z")
    report = await sync.run()
    assert library.drive_pending() == []
    active = store.active_documents()
    assert len(active) == 1 and "5 жолудів" in active[0].text and active[0].scope == ["pid_dubom"]
    # the clarified file's older pending version is closed in the records, not left "pending"
    assert [d.status for d in store.documents_for_drive_file("a")] == [st.SUPERSEDED, st.SUPERSEDED, st.ACTIVE]


async def test_unsupported_and_unreadable_files_are_listed_in_pending(sync, drive, store, library):
    drive.put("img", "фото.jpg", b"\xff\xd8\xff", "2026-10-01T10:00:00Z", mime="image/jpeg")
    drive.put("bad", "Зламаний.pdf", b"%PDF-1.4 broken", "2026-10-01T10:00:00Z", mime="application/pdf")
    report = await sync.run()
    assert report.pending == 2
    reasons = {d.title: d.status_reason for d in library.drive_pending()}
    assert "не підтримується" in reasons["фото"]
    assert "PDF" in reasons["Зламаний"]
    again = await sync.run()                      # unchanged -> not re-processed every time
    assert again.unchanged == 2 and again.pending == 0


async def test_deleted_pending_file_disappears_from_pending(sync, drive, library):
    drive.put("img", "фото.jpg", b"\xff\xd8\xff", "2026-10-01T10:00:00Z", mime="image/jpeg")
    drive.put("ok", "Календар.txt", "Календар\nЦей календар для всіх мешканців лісу.", "2026-10-01T10:00:00Z")
    await sync.run()
    drive.delete("img")
    await sync.run()
    assert library.drive_pending() == []


async def test_google_throttling_defers_remaining_downloads_without_damage(archive, sync, drive, store):
    from forest_bot.drive import DriveThrottled
    for fid in list(drive.files)[:5]:
        f, data = drive.files[fid]
        drive.put(fid, f.name, data.decode() + "\nДоповнення.", "2026-10-20T10:00:00Z")

    async def blocked(f):
        raise DriveThrottled("automated queries")

    real = drive.download
    drive.download = blocked
    report = await sync.run()
    assert sync.throttled and report.errors == 5 and report.updated == 0 and report.removed == 0
    assert len(store.active_documents()) == 78
    drive.download = real
    report = await sync.run()                         # next sync picks the deferred files up
    assert not sync.throttled and report.updated == 5


def test_throttle_retry_backoff_is_exponential_and_bounded():
    from forest_bot.drive import throttle_retry_delay
    assert [throttle_retry_delay(n, 300, 3600) for n in range(1, 7)] == [300, 600, 1200, 2400, 3600, 3600]


async def test_throttle_streak_grows_while_blocked_and_resets_after_a_clean_run(archive, sync, drive, store):
    from forest_bot.drive import DriveThrottled
    for fid in list(drive.files)[:3]:
        f, data = drive.files[fid]
        drive.put(fid, f.name, data.decode() + "\nДоповнення.", "2026-10-20T10:00:00Z")

    async def blocked(f):
        raise DriveThrottled("automated queries")

    real = drive.download
    drive.download = blocked
    await sync.run()
    await sync.run()
    assert sync.throttle_streak == 2 and len(store.active_documents()) == 78     # nothing lost meanwhile
    drive.download = real
    report = await sync.run()
    assert sync.throttle_streak == 0 and report.updated == 3


async def test_transient_http_errors_use_bounded_retries_honouring_retry_after():
    import httpx
    from forest_bot.drive import DriveClient, DriveError
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, headers={"retry-after": "0"}, text="busy")
        return httpx.Response(200, content=b"ok")

    client = DriveClient("key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    resp = await client._get("https://www.googleapis.com/drive/v3/files/x", {"alt": "media"})
    assert resp.content == b"ok" and len(calls) == 3

    calls.clear()
    always = DriveClient("key")
    always._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (calls.append(1), httpx.Response(503, headers={"retry-after": "0"}))[1]))
    try:
        await always._get("https://www.googleapis.com/drive/v3/files/x", {"alt": "media"})
    except DriveError:
        pass
    assert len(calls) == 4                                                     # bounded


async def test_downloads_use_the_official_drive_api_endpoints():
    import httpx
    from forest_bot.drive import DriveClient, DriveFile
    seen = []

    def record(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x")

    client = DriveClient("SECRET")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    await client.download(DriveFile("pdf1", "a.pdf", "application/pdf", "2026-10-18T00:00:00+00:00"))
    await client.download(DriveFile("doc1", "Нотатки", "application/vnd.google-apps.document",
                                    "2026-10-18T00:00:00+00:00"))
    assert seen[0].startswith("https://www.googleapis.com/drive/v3/files/pdf1?") and "alt=media" in seen[0]
    assert seen[1].startswith("https://www.googleapis.com/drive/v3/files/doc1/export?")
    assert not any("drive.google.com" in u or "usercontent" in u for u in seen)


async def test_long_retry_after_is_not_cut_short_and_defers_the_attempt():
    import time
    import httpx
    from forest_bot.drive import DriveClient, DriveRetryLater
    calls = []

    def limited(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "120"})

    client = DriveClient("key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(limited))
    started = time.monotonic()
    try:
        await client._get("https://www.googleapis.com/drive/v3/files/x", {"alt": "media"})
    except DriveRetryLater as exc:
        assert exc.retry_after == 120
    else:
        raise AssertionError("expected DriveRetryLater")
    assert len(calls) == 1 and time.monotonic() - started < 1       # no early retry, no blocking wait


async def test_retry_after_on_download_defers_and_blocks_syncs_until_the_requested_time(archive, sync, drive,
                                                                                    store):
    from forest_bot.drive import DriveRetryLater
    fid = next(iter(drive.files))
    f, data = drive.files[fid]
    drive.put(fid, f.name, data.decode() + "\nДоповнення.", "2026-10-20T10:00:00Z")
    real = drive.download

    async def ask_to_wait(f):
        raise DriveRetryLater(120)

    drive.download = ask_to_wait
    report = await sync.run()
    assert report.errors == 1 and sync.retry_after == 120 and len(store.active_documents()) == 78
    listings = drive.listings
    skipped = await sync.run()                                   # e.g. the regular 10-minute job
    assert not skipped.ok and "відкладено" in skipped.message and drive.listings == listings
    sync.not_before = 0                                          # requested time has passed
    drive.download = real
    report = await sync.run()
    assert report.updated == 1 and sync.retry_after == 0


async def test_retry_after_on_listing_changes_nothing(archive, sync, drive, store):
    from forest_bot.drive import DriveRetryLater

    async def ask_to_wait(folder_id):
        raise DriveRetryLater(300)

    drive.list_folder = ask_to_wait
    report = await sync.run()
    assert not report.ok and sync.retry_after == 300 and len(store.active_documents()) == 78
