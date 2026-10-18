"""Document identity, versioning, timestamps, withdrawal — enforced by application logic."""

import pytest

from forest_bot import storage as st
from forest_bot.retrieval import QueryScope, Retriever

from .conftest import ts

FEES_DUB = ("Членські внески рукавички «Під дубом»\n"
            "Шановні мешканці рукавички «Під дубом»!\nВнесок — 4 жолуді з дорослого звіра на місяць. "
            "Здавати до 10 числа Кабанові.")
FEES_DUB_V2 = FEES_DUB.replace("4 жолуді", "5 жолудів")
AWAY_DUB = ("Як платити, коли тебе немає вдома\n"
            "Для мешканців рукавички «Під дубом»: якщо ви в мандрах, внесок можна передати через Сову.")


async def drive_doc(library, file_id, name, text, modified):
    return await library.ingest_drive_file(file_id=file_id, name=name, modified_time=ts(modified).isoformat(),
                                           link=f"https://drive.google.com/file/d/{file_id}/view",
                                           data=text.encode("utf-8"))


async def tg_doc(library, user_id, name, text, received, uploader="Олена Коваль"):
    doc = library.create_telegram_upload(user_id=user_id, chat_id=user_id, tg_file_id=f"tg-{name}-{received}",
                                         filename=name, received_at=ts(received), uploader_name=uploader)
    return await library.process_telegram_file(doc.id, text.encode("utf-8"))


def active_titles(store):
    return sorted(d.title for d in store.active_documents())


async def test_same_title_and_scope_newer_drive_file_replaces_older(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    b = await drive_doc(library, "B", "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-05T10:00:00Z")
    assert a.kind == "activated" and b.kind == "replaced"
    assert store.get_document(a.doc.id).status == st.SUPERSEDED
    assert store.get_document(a.doc.id).superseded_by == b.doc.id
    assert store.get_document(b.doc.id).logical_id == store.get_document(a.doc.id).logical_id


async def test_older_version_never_replaces_newer(library, store):
    new = await drive_doc(library, "B", "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-05T10:00:00Z")
    old = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    assert old.kind == "older"
    assert store.get_document(new.doc.id).status == st.ACTIVE
    assert store.get_document(old.doc.id).status == st.SUPERSEDED


async def test_version_order_uses_source_timestamp_not_ingest_time(library, store):
    # Telegram upload received at 12:00, but a Drive edit made at 13:00 is ingested later -> Drive wins.
    t = await tg_doc(library, 1, "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-10T12:00:00Z")
    d = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-10T13:00:00Z")
    assert d.kind == "replaced" and store.get_document(t.doc.id).status == st.SUPERSEDED
    # A Drive file last modified before the Telegram receipt time does not replace it.
    t2 = await tg_doc(library, 1, "Членські внески рукавички Під дубом.txt",
                      FEES_DUB.replace("4 жолуді", "7 жолудів"), "2026-10-11T12:00:00Z")
    old_drive = await drive_doc(library, "Z", "Членські внески рукавички Під дубом.txt",
                                FEES_DUB.replace("4 жолуді", "9 жолудів"), "2026-10-09T08:00:00Z")
    assert t2.kind == "replaced" and old_drive.kind == "older"
    assert [d.id for d in store.active_documents()] == [t2.doc.id]


async def test_same_topic_same_place_different_title_is_complementary(library, store):
    await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    away = await drive_doc(library, "B", "Як платити, коли тебе немає вдома.txt", AWAY_DUB, "2026-10-05T10:00:00Z")
    assert away.kind == "activated"
    assert set(active_titles(store)) == {"Як платити, коли тебе немає вдома", "Членські внески рукавички Під дубом"}


async def test_same_title_different_place_is_separate(library, store):
    await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    other = await drive_doc(library, "B", "Членські внески рукавички У ліщині.txt",
                            FEES_DUB.replace("Під дубом", "У ліщині"), "2026-10-05T10:00:00Z")
    assert other.kind == "activated" and len(store.active_documents()) == 2


async def test_explicit_update_statement_naming_document_replaces(library, store):
    await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    text = ("Нові правила внесків\nЦей документ замінює «Членські внески рукавички Під дубом». "
            "Для мешканців рукавички «Під дубом»: внесок 6 жолудів до 15 числа.")
    out = await tg_doc(library, 7, "nove.txt", text, "2026-10-12T09:00:00Z")
    assert out.kind == "replaced" and out.other.title == "Членські внески рукавички Під дубом"


async def test_reingesting_unchanged_drive_content_creates_no_version(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    again = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-08T10:00:00Z")
    assert again.kind == "duplicate"
    active = store.active_documents()
    assert [d.id for d in active] == [a.doc.id]
    assert active[0].version_ts == a.doc.version_ts      # not made newer


async def test_telegram_duplicate_of_active_document(library, store):
    await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    out = await tg_doc(library, 5, "copy.txt", FEES_DUB, "2026-10-12T09:00:00Z")
    assert out.kind == "duplicate" and len(store.active_documents()) == 1


async def test_telegram_upload_may_replace_drive_document(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    out = await tg_doc(library, 5, "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-12T09:00:00Z")
    assert out.kind == "replaced" and out.other.id == a.doc.id
    assert store.get_document(a.doc.id).status == st.SUPERSEDED


async def test_telegram_upload_may_replace_another_users_document(library, store):
    first = await tg_doc(library, 1, "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-10T09:00:00Z",
                         uploader="Іван")
    second = await tg_doc(library, 2, "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-11T09:00:00Z",
                          uploader="Марія")
    assert second.kind == "replaced" and second.other.id == first.doc.id


async def test_ambiguous_telegram_identity_asks_and_answer_is_applied(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    similar = FEES_DUB_V2.replace("Членські внески рукавички «Під дубом»", "Членські внески у рукавичці «Під дубом»", 1)
    out = await tg_doc(library, 5, "x.txt", similar, "2026-10-12T09:00:00Z")
    assert out.kind == "need_replacement" and out.other.id == a.doc.id
    assert out.doc.status == st.PENDING_REPLACEMENT
    assert out.doc.id not in [d.id for d in store.active_documents()]     # not used while unresolved
    res = await library.resolve_replacement(out.doc.id, replaces=True)
    assert res.kind == "replaced" and store.get_document(a.doc.id).status == st.SUPERSEDED


async def test_ambiguous_telegram_identity_answered_as_separate(library, store):
    await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    similar = FEES_DUB_V2.replace("Членські внески рукавички «Під дубом»", "Членські внески у рукавичці «Під дубом»", 1)
    out = await tg_doc(library, 5, "x.txt", similar, "2026-10-12T09:00:00Z")
    res = await library.resolve_replacement(out.doc.id, replaces=False)
    assert res.kind == "activated" and len(store.active_documents()) == 2


async def test_confirmed_replacement_still_checks_timestamps(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-12-01T10:00:00Z")
    similar = FEES_DUB_V2.replace("Членські внески рукавички «Під дубом»", "Членські внески у рукавичці «Під дубом»", 1)
    out = await tg_doc(library, 5, "x.txt", similar, "2026-10-12T09:00:00Z")   # received before the Drive edit
    res = await library.resolve_replacement(out.doc.id, replaces=True)
    assert res.kind == "older" and store.get_document(a.doc.id).status == st.ACTIVE


async def test_ambiguous_drive_identity_keeps_both_documents(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    similar = FEES_DUB_V2.replace("Членські внески рукавички «Під дубом»", "Членські внески у рукавичці «Під дубом»", 1)
    b = await drive_doc(library, "B", "Членські внески у рукавичці Під дубом.txt", similar, "2026-10-05T10:00:00Z")
    assert b.kind == "kept_separate"
    assert {a.doc.id, b.doc.id} == {d.id for d in store.active_documents()}
    assert any(h["event"] == "possible_replacement_not_applied" for h in store.get_document(b.doc.id).history)


async def test_withdrawal_ownership_and_no_restore(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    t = await tg_doc(library, 5, "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-12T09:00:00Z")
    with pytest.raises(PermissionError):
        library.withdraw(t.doc.id, user_id=6)
    with pytest.raises(PermissionError):
        library.withdraw(a.doc.id, user_id=5)          # Drive documents cannot be withdrawn
    library.withdraw(t.doc.id, user_id=5)
    assert store.get_document(t.doc.id).status == st.WITHDRAWN
    assert store.get_document(a.doc.id).status == st.SUPERSEDED   # not reactivated
    assert store.active_documents() == []


async def test_unresolved_superseded_withdrawn_removed_documents_are_never_retrieved(library, store):
    old = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    new = await drive_doc(library, "B", "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-05T10:00:00Z")
    pending = await drive_doc(library, "C", "Нотатки.txt", "Нотатки\nВнесок 99 жолудів до 1 числа.",
                              "2026-10-06T10:00:00Z")
    tg = await tg_doc(library, 5, "Внески восени.txt",
                      "Внески восени\nДля мешканців рукавички «Під дубом»: восени внесок 8 жолудів.",
                      "2026-10-07T10:00:00Z")
    assert pending.kind == "need_location"
    retriever = Retriever(store)
    q = QueryScope("mittens", ["pid_dubom"])

    def retrieved_ids():
        return {d.document.id for d in retriever.retrieve("внесок жолуді", q, library.revision).docs}

    assert retrieved_ids() == {new.doc.id, tg.doc.id}
    library.withdraw(tg.doc.id, 5)
    assert retrieved_ids() == {new.doc.id}
    library.remove_drive_file("B")
    assert retrieved_ids() == set()          # old superseded version is not restored
    assert store.get_document(old.doc.id).status == st.SUPERSEDED


async def test_unreadable_replacement_keeps_previous_version(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    bad = await library.ingest_drive_file(file_id="A", name="Членські внески рукавички Під дубом.pdf",
                                          modified_time=ts("2026-10-05T10:00:00Z").isoformat(),
                                          link="l", data=b"%PDF-broken")
    assert bad.kind == "failed"
    assert [d.id for d in store.active_documents()] == [a.doc.id]


async def test_decisions_are_recorded_for_provenance(library, store):
    a = await drive_doc(library, "A", "Членські внески рукавички Під дубом.txt", FEES_DUB, "2026-10-01T10:00:00Z")
    b = await drive_doc(library, "B", "Членські внески рукавички Під дубом.txt", FEES_DUB_V2, "2026-10-05T10:00:00Z")
    hist = store.get_document(b.doc.id).history
    replaced = next(h for h in hist if h["event"] == "replaced")
    assert replaced["previous"] == a.doc.id and replaced["previous_ts"] < replaced["new_ts"]
    assert any(h["event"] == "scope" and h["method"] == "title" for h in hist)


async def test_title_contained_in_another_title_for_same_place_is_ambiguous(library, store):
    await drive_doc(library, "A", "Правила купання.txt",
                    "Правила купання\nДля мешканців рукавички «Під вербою»: купатися вдень.", "2026-10-01T10:00:00Z")
    out = await tg_doc(library, 5, "y.txt", "Правила купання у рукавичці\nДля мешканців рукавички «Під вербою»: "
                                             "після купання обтрусіться.", "2026-10-12T09:00:00Z")
    assert out.kind == "need_replacement"
    # different place -> unrelated, no question
    other = await tg_doc(library, 5, "z.txt", "Правила купання у рукавичці\nДля мешканців рукавички «Під дубом»: "
                                               "купатися лише в ставку.", "2026-10-12T10:00:00Z")
    assert other.kind == "activated"
