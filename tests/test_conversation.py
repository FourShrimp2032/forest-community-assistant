"""Dialogue flows through the transport-independent Conversation (Telegram replaced by direct calls)."""

from datetime import datetime, timezone

from forest_bot import messages as msg
from forest_bot import storage as st

from .conftest import evidence_titles

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
DOC_DUB = ("Членські внески рукавички «Під дубом»\nДля мешканців рукавички «Під дубом»: внесок 4 жолуді до 10 числа.")
DOC_NO_PLACE = "Нотатки про внески\nВнесок 3 горіхи на місяць, до 7 числа."


async def upload(conv, fake_files, user, name, text, when=NOW):
    file_id = f"tg-{user}-{name}-{when.timestamp()}"
    fake_files[file_id] = text.encode("utf-8")
    return await conv.on_document(user_id=user, chat_id=user, file_id=file_id, filename=name, size=len(text),
                                  received_at=when)


def texts(replies):
    return "\n".join(r.text for r in replies)


def callbacks(replies):
    return [b.data for r in replies for row in r.buttons for b in row if b.data]


# ------------------------------------------------------------------ real name

async def test_first_upload_asks_real_name_then_processes_pending_upload(conversation, fake_files, store):
    replies = await upload(conversation, fake_files, 1, "внески.txt", DOC_DUB)
    assert "справжнє ім'я" in texts(replies)
    pending = store.documents_by_uploader(1)[0]
    assert pending.status == st.AWAITING_NAME                        # upload preserved, not yet used
    assert store.active_documents() == []
    replies = await conversation.on_text(1, "Олена Коваль")
    assert "Дякую, Олена Коваль" in texts(replies)
    doc = store.get_document(pending.id)
    assert doc.status == st.ACTIVE and doc.uploader_name == "Олена Коваль" and doc.uploader_id == 1
    assert doc.version_ts.startswith("2026-10-15T09:00:00")          # Telegram receipt time


async def test_name_is_remembered_for_next_uploads(conversation, fake_files, store):
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    await conversation.on_text(1, "Олена Коваль")
    replies = await upload(conversation, fake_files, 1, "b.txt",
                           "Правила купання\nДля мешканців рукавички «Під вербою»: купатися вдень.")
    assert "справжнє ім'я" not in texts(replies) and "додано" in texts(replies)


async def test_telegram_profile_name_is_not_used_silently(conversation, fake_files, store):
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    assert store.get_user(1)["real_name"] is None
    assert store.documents_by_uploader(1)[0].uploader_name is None


async def test_questions_never_require_a_name(conversation, store, archive):
    replies = await conversation.on_text(9, "Скільки внесок під дубом?")
    assert "ім'я" not in texts(replies)
    assert store.get_user(9)["real_name"] is None


async def test_question_while_waiting_for_name_is_answered_with_reminder(conversation, fake_files, archive):
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    replies = await conversation.on_text(1, "Скільки внесок під дубом?")
    assert "Джерела" in texts(replies) and "потрібне ваше справжнє ім'я" in texts(replies)


async def test_invalid_name_is_rejected_by_heuristics(conversation):
    from forest_bot.conversation import looks_like_name
    assert looks_like_name("Олена Коваль") and looks_like_name("Іван")
    assert not looks_like_name("Скільки внесок?") and not looks_like_name("12345")
    assert not looks_like_name("під дубом")


# ------------------------------------------------------------------ document location

async def test_unclear_location_asks_and_document_is_unused_until_resolved(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    replies = await upload(conversation, fake_files, 1, "notes.txt", DOC_NO_PLACE)
    assert "Не можу однозначно визначити" in texts(replies)
    doc = store.documents_by_uploader(1)[0]
    assert doc.status == st.PENDING_LOCATION and store.active_documents() == []
    assert f"dl:t:{doc.id}:pid_dubom" in callbacks(replies)
    # multi-select with buttons
    await conversation.on_callback(1, f"dl:t:{doc.id}:pid_dubom")
    await conversation.on_callback(1, f"dl:t:{doc.id}:u_lishchyni")
    replies = await conversation.on_callback(1, f"dl:ok:{doc.id}")
    doc = store.get_document(doc.id)
    assert doc.status == st.ACTIVE and doc.scope == ["pid_dubom", "u_lishchyni"] and doc.scope_method == "user"


async def test_location_answer_by_text_region_and_whole_forest(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    await upload(conversation, fake_files, 1, "a.txt", DOC_NO_PLACE)
    await conversation.on_text(1, "хвойний ліс")
    assert store.documents_by_uploader(1)[0].scope == ["khvoinyi"]
    await upload(conversation, fake_files, 1, "b.txt", "Інші нотатки\nВсі збираються на галявині.")
    await conversation.on_text(1, "увесь ліс")
    assert store.documents_by_uploader(1)[0].scope == ["forest"]


async def test_other_user_cannot_answer_someone_elses_clarification(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    await upload(conversation, fake_files, 1, "a.txt", DOC_NO_PLACE)
    doc = store.documents_by_uploader(1)[0]
    replies = await conversation.on_callback(2, f"dl:ok:{doc.id}")
    assert store.get_document(doc.id).status == st.PENDING_LOCATION
    assert replies[0].toast


# ------------------------------------------------------------------ replacement question

async def test_ambiguous_replacement_question_identifies_candidate(conversation, fake_files, store, library):
    store.set_user_name(1, "Іван")
    store.set_user_name(2, "Марія")
    await upload(conversation, fake_files, 1, "Членські внески рукавички Під дубом.txt", DOC_DUB)
    similar = DOC_DUB.replace("Членські внески рукавички «Під дубом»", "Членські внески у рукавичці «Під дубом»", 1)
    replies = await upload(conversation, fake_files, 2, "x.txt", similar.replace("4 жолуді", "5 жолудів"),
                           when=datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc))
    text = texts(replies)
    assert "«Членські внески рукавички «Під дубом»» — надіслано: Іван" in text
    new_doc = store.documents_by_uploader(2)[0]
    assert f"rp:y:{new_doc.id}" in callbacks(replies)
    replies = await conversation.on_callback(2, f"rp:y:{new_doc.id}")
    assert store.get_document(new_doc.id).status == st.ACTIVE
    assert store.documents_by_uploader(1)[0].status == st.SUPERSEDED


# ------------------------------------------------------------------ /my_documents & withdrawal

async def test_my_documents_withdrawal_requires_confirmation_and_ownership(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    doc = store.documents_by_uploader(1)[0]
    page = await conversation.on_command(1, "/my_documents")
    assert f"md:w:{doc.id}:0" in callbacks(page)
    assert "використовується" in texts(page)
    # another user sees nothing and cannot withdraw
    assert msg.MY_DOCS_EMPTY in texts(await conversation.on_command(2, "/my_documents"))
    assert msg.NOT_YOUR_DOCUMENT in texts(await conversation.on_callback(2, f"md:wy:{doc.id}:0"))
    assert store.get_document(doc.id).status == st.ACTIVE
    # owner: ask -> cancel -> ask -> confirm
    confirm = await conversation.on_callback(1, f"md:w:{doc.id}:0")
    assert f"md:wy:{doc.id}:0" in callbacks(confirm)
    await conversation.on_callback(1, f"md:wn:{doc.id}:0")
    assert store.get_document(doc.id).status == st.ACTIVE
    await conversation.on_callback(1, f"md:wy:{doc.id}:0")
    assert store.get_document(doc.id).status == st.WITHDRAWN


async def test_file_button_after_withdrawal(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    doc = store.documents_by_uploader(1)[0]
    replies = await conversation.on_callback(5, f"f:{doc.id}")
    assert replies[0].document_file_id == doc.tg_file_id
    await conversation.on_callback(1, f"md:wy:{doc.id}:0")
    replies = await conversation.on_callback(5, f"f:{doc.id}")
    assert replies[0].text == "Документ відкликано." and replies[0].document_file_id is None


async def test_my_documents_pagination(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    for i in range(7):
        await upload(conversation, fake_files, 1, f"d{i}.txt", f"Документ {i}\nДля всіх мешканців лісу: порада {i}.")
    first = await conversation.on_command(1, "/my_documents")
    assert "сторінка 1 з 3" in texts(first) and "md:p:1" in callbacks(first)
    second = await conversation.on_callback(1, "md:p:1")
    assert second[0].edit and "сторінка 2 з 3" in second[0].text


# ------------------------------------------------------------------ questions

async def test_question_location_clarification_with_buttons(conversation, archive):
    conversation.llm.route = {"intent": "question", "locations": [], "needs_location": True,
                              "search_query": "Скільки внесок?", "keywords": ["внесок"]}
    replies = await conversation.on_text(3, "Скільки внесок?")
    assert replies[0].text == msg.ASK_QUESTION_LOCATION
    assert "ql:pid_dubom" in callbacks(replies) and "ql:forest" in callbacks(replies)
    replies = await conversation.on_callback(3, "ql:pid_dubom")
    assert "Джерела" in texts(replies)
    assert evidence_titles(conversation.llm.answer_prompts[-1])[0] == "Членські внески рукавички Під дубом"


async def test_follow_up_keeps_location_and_reset_clears_it(conversation, archive, store):
    conversation.llm.route = {"intent": "question", "locations": [], "needs_location": False,
                              "search_query": "внесок", "keywords": ["внесок"]}
    await conversation.on_text(3, "Скільки внесок під дубом?")
    conversation.llm.route = {"intent": "question", "locations": [], "needs_location": True,
                              "search_query": "До якого числа здавати внесок?", "keywords": ["числа", "внесок"]}
    await conversation.on_text(3, "А до якого числа?")
    assert "Під дубом" in conversation.llm.answer_prompts[-1].split("Документи:")[0]
    replies = await conversation.on_command(3, "/reset")
    assert msg.RESET_DONE in texts(replies)
    replies = await conversation.on_text(3, "А до якого числа?")
    assert replies[0].text == msg.ASK_QUESTION_LOCATION


async def test_reset_keeps_name_and_documents(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    await upload(conversation, fake_files, 1, "a.txt", DOC_DUB)
    await conversation.on_command(1, "/reset")
    assert store.get_user(1)["real_name"] == "Іван"
    assert len(store.active_documents()) == 1


async def test_offtopic_reply(conversation):
    conversation.llm.route = {"intent": "offtopic"}
    replies = await conversation.on_text(3, "Напиши вірш про море")
    assert replies[0].text == msg.OFFTOPIC


async def test_rate_limit_message(conversation, archive):
    from forest_bot.llm import LLMRateLimited

    async def limited(*a, **k):
        raise LLMRateLimited(60)

    conversation.llm.chat_json = limited
    replies = await conversation.on_text(3, "Скільки внесок під дубом?")
    assert replies[0].text == msg.RATE_LIMITED


async def test_unsupported_upload_formats(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    replies = await upload(conversation, fake_files, 1, "old.doc", "x")
    assert "не підтримується" in texts(replies)
    replies = await conversation.on_document(user_id=1, chat_id=1, file_id="big", filename="big.pdf",
                                             size=50 * 1024 * 1024, received_at=NOW)
    assert "завеликий" in texts(replies)


async def test_unreadable_upload_notifies_sender(conversation, fake_files, store):
    store.set_user_name(1, "Іван")
    replies = await upload(conversation, fake_files, 1, "broken.pdf", "%PDF-broken")
    assert "Не вдалося прочитати" in texts(replies)
    assert store.documents_by_uploader(1)[0].status == st.FAILED and store.active_documents() == []


# ------------------------------------------------------------------ /pending

async def test_pending_lists_drive_documents_with_link_and_reason(conversation, drive, sync):
    drive.put("p1", "Нотатки.txt", "Нотатки\nВнесок 3 горіхи.", "2026-10-01T10:00:00Z")
    await sync.run()
    replies = await conversation.on_command(1, "/pending")
    text = texts(replies)
    assert "Нотатки" in text and "https://drive.google.com/file/d/p1/view" in text and "Причина" in text
