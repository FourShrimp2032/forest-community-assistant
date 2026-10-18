"""Document-upload lifecycle end to end, several users, file-based database (offline)."""

import re
from datetime import datetime, timezone

from forest_bot import messages as msg
from forest_bot import storage as st
from forest_bot.library import Library
from forest_bot.storage import Store

from .conftest import FakeLLM, evidence_titles, make_conversation

ALICE, BOHDAN, CHRYSTIA = 1001, 1002, 1003
SWIM_AMBIGUOUS = "Правила купання\nКупатися можна лише вдень і лише вдвох. Біля заводі глибоко."
SWIM_V2 = ("Правила купання\nДля мешканців рукавичок «Під вербою» і «Біля бобрової греблі»: купатися лише вдень, "
           "лише вдвох і ніколи не в заводі.")
SWIM_OTHER = ("Правила купання у рукавичці\nДля мешканців рукавичок «Під вербою» і «Біля бобрової греблі»: "
              "після купання обтрусіться на корінні.")


def t(hour):
    return datetime(2026, 10, 18, hour, 0, tzinfo=timezone.utc)


def build(db_path, files_dir, uploads):
    store = Store(db_path)
    library = Library(store, files_dir)
    llm = FakeLLM()
    conv = make_conversation(store, library, llm)
    conv.llm = llm

    async def downloader(file_id):
        return uploads[file_id]

    conv.downloader = downloader
    return store, library, conv


async def upload(conv, uploads, user, name, text, when):
    file_id = f"tg-{user}-{name}-{when.isoformat()}"
    uploads[file_id] = text.encode()
    return await conv.on_document(user_id=user, chat_id=user, file_id=file_id, filename=name, size=len(text),
                                  received_at=when)


def texts(replies):
    return "\n".join(r.text for r in replies)


def buttons(replies):
    return [b.data for r in replies for row in r.buttons for b in row if b.data]


async def test_upload_lifecycle_end_to_end(tmp_path):
    uploads = {}
    db = tmp_path / "forest.sqlite3"
    store, library, conv = build(db, tmp_path / "files", uploads)

    # 1. First upload: the real name is requested; the upload waits and is not used yet.
    r = await upload(conv, uploads, ALICE, "купання.txt", SWIM_AMBIGUOUS, t(7))
    assert "справжнє ім'я" in texts(r)
    a1 = store.documents_by_uploader(ALICE)[0]
    assert a1.status == st.AWAITING_NAME and store.active_documents() == []

    # 2. Name given -> processing resumes -> location is ambiguous -> sender is asked.
    r = await conv.on_text(ALICE, "Аліна Вербова")
    assert "Дякую, Аліна Вербова" in texts(r) and "Не можу однозначно визначити" in texts(r)
    assert store.get_document(a1.id).status == st.PENDING_LOCATION and store.active_documents() == []

    # 3. Clarified with buttons (two mittens) -> active.
    await conv.on_callback(ALICE, f"dl:t:{a1.id}:pid_verboyu")
    await conv.on_callback(ALICE, f"dl:t:{a1.id}:bilia_hrebli")
    r = await conv.on_callback(ALICE, f"dl:ok:{a1.id}")
    a1 = store.get_document(a1.id)
    assert a1.status == st.ACTIVE and a1.scope == ["pid_verboyu", "bilia_hrebli"]
    assert a1.uploader_name == "Аліна Вербова" and a1.version_ts.startswith("2026-10-18T07:00:00")

    # 4. Another user (no name needed) gets answers from it, with stored provenance and a file button.
    r = await conv.on_text(BOHDAN, "Коли можна купатися під вербою?")
    html = r[-1].text
    assert "Правила купання" in evidence_titles(conv.llm.answer_prompts[-1])
    assert "надіслано: Аліна Вербова, 18.10.2026 10:00 (Київ)" in html          # UTC 07:00 -> Kyiv 10:00
    assert f"f:{a1.id}" in buttons(r)
    assert store.get_user(BOHDAN)["real_name"] is None
    # …but not for an unrelated mitten.
    prompts_before = len(conv.llm.answer_prompts)
    r = await conv.on_text(CHRYSTIA, "Коли можна купатися під дубом?")
    assert len(conv.llm.answer_prompts) == prompts_before       # nothing applicable -> no evidence, no LLM call
    assert "не знайшлося" in r[-1].text and "Правила купання" not in r[-1].text
    file_reply = await conv.on_callback(BOHDAN, f"f:{a1.id}")
    assert file_reply[0].document_file_id == a1.tg_file_id

    # 5. Bohdan uploads a newer version (same title + same place): cross-user replacement.
    r = await upload(conv, uploads, BOHDAN, "Правила купання.txt", SWIM_V2, t(9))
    assert "справжнє ім'я" in texts(r)                                 # his first upload -> name requested
    assert store.get_document(a1.id).status == st.ACTIVE               # nothing replaced before processing
    r = await conv.on_text(BOHDAN, "Богдан Греблюк")
    b1 = store.documents_by_uploader(BOHDAN)[0]
    assert b1.status == st.ACTIVE and "нову версію документа «Правила купання»" in texts(r)
    assert store.get_document(a1.id).status == st.SUPERSEDED
    await conv.on_text(CHRYSTIA, "Коли можна купатися під вербою?")
    prompt = conv.llm.answer_prompts[-1]
    assert "ніколи не в заводі" in prompt and "Біля заводі глибоко" not in prompt   # superseded not retrieved

    # 6. A similar title for the same place: the uploader is asked; "separate" keeps both.
    r = await upload(conv, uploads, BOHDAN, "y.txt", SWIM_OTHER, t(10))
    b2 = store.documents_by_uploader(BOHDAN)[0]
    assert b2.status == st.PENDING_REPLACEMENT and f"rp:n:{b2.id}" in buttons(r)
    await conv.on_callback(BOHDAN, f"rp:n:{b2.id}")
    assert store.get_document(b2.id).status == st.ACTIVE and store.get_document(b1.id).status == st.ACTIVE

    # 7. Withdrawal: only the owner, with confirmation; no restore of the superseded version.
    assert msg.NOT_YOUR_DOCUMENT in texts(await conv.on_callback(ALICE, f"md:wy:{b1.id}:0"))
    assert store.get_document(b1.id).status == st.ACTIVE
    confirm = await conv.on_callback(BOHDAN, f"md:w:{b1.id}:0")
    assert f"md:wy:{b1.id}:0" in buttons(confirm)
    await conv.on_callback(BOHDAN, f"md:wy:{b1.id}:0")
    assert store.get_document(b1.id).status == st.WITHDRAWN
    assert store.get_document(a1.id).status == st.SUPERSEDED            # not reactivated
    prompts_before = len(conv.llm.answer_prompts)
    await conv.on_text(CHRYSTIA, "Коли можна купатися під вербою?")
    assert len(conv.llm.answer_prompts) == prompts_before + 1          # this question reached the model
    titles = evidence_titles(conv.llm.answer_prompts[-1])
    assert "Правила купання у рукавичці" in titles and "Правила купання" not in titles
    assert (await conv.on_callback(CHRYSTIA, f"f:{b1.id}"))[0].text == "Документ відкликано."

    # 8. /my_documents shows each user's own uploads with their status.
    alice_docs = texts(await conv.on_command(ALICE, "/my_documents"))
    assert "замінено новішою версією" in alice_docs and "Правила купання у рукавичці" not in alice_docs
    bohdan_docs = texts(await conv.on_command(BOHDAN, "/my_documents"))
    assert "відкликано" in bohdan_docs and "використовується" in bohdan_docs

    # 9. Restart: names, documents and statuses persist; the name is not asked again.
    store.close()
    store, library, conv = build(db, tmp_path / "files", uploads)
    assert store.get_user(ALICE)["real_name"] == "Аліна Вербова"
    assert store.get_document(b1.id).status == st.WITHDRAWN and store.get_document(b2.id).status == st.ACTIVE
    r = await upload(conv, uploads, ALICE, "Правила тиші.txt",
                     "Правила тиші\nДля мешканців рукавички «Під вербою»: тиша після заходу сонця.", t(11))
    assert "справжнє ім'я" not in texts(r) and "додано" in texts(r)
    assert not re.search(r"ім'я", texts(r))
