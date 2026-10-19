# Лісовий помічник — Forest Community Document Assistant

A Telegram bot for the fictional animal communities of the forest. Each «рукавичка» (mitten) is a separate
homeowners' association (ОСМД). The bot answers questions from shared documents and shows their sources.
It understands geographic scope (mitten → forest region → whole forest), accepts new documents over
Telegram, synchronises the Google Drive archive «Лісовий архів», and manages document versions.

User-facing messages are in Ukrainian. See also:

- [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md): the agreed behaviour;
- [`docs/PLAN.md`](docs/PLAN.md): the implementation plan;
- [`docs/VERIFICATION.md`](docs/VERIFICATION.md): dated verification evidence.

---

## Quick start

```bash
cp .env.example .env        # fill TELEGRAM_BOT_TOKEN, GROQ_API_KEY, GOOGLE_DRIVE_API_KEY (see Credentials)
docker compose up --build
```

What happens on start, in this order (`forest_bot/__main__.py`):

1. **Startup checks:**
   - create or open the SQLite database and file store in the volume `forest-data` (`/data`);
   - mark any upload interrupted by a previous restart as failed;
   - **on an empty database only:** import the bundled snapshot of «Лісовий архів» (see
     [Bundled archive snapshot](#bundled-archive-snapshot)). This needs no network: the stored text is
     reused, so nothing is downloaded or OCRed;
   - build the search index;
   - check that the configured Groq models are available.

   **Local archive readiness, measured once** (developer's Mac, 18 Oct 2026, Docker, empty volume, no
   network): the 78 documents were searchable about **7–13 s after process start** (7.3 s and 12.4 s in two
   fresh runs; 2.1 s after a restart). This excludes the image build and the Telegram connection, and it is
   an observation on one machine, not a startup guarantee.
2. **Telegram polling starts.** The bot answers from this point on.
3. **The first Drive synchronisation starts about 1 s later** as a background job. It does **not** block
   polling. On a snapshot-based installation it normally only lists the folder: unchanged files are not
   downloaded again, and only new or changed files are downloaded and ingested. Later syncs run every
   `DRIVE_SYNC_INTERVAL_SECONDS` (10 min).

**Until Drive has been checked,** answers from the snapshot start with «ℹ️ Відповідь ґрунтується на
локальній копії «Лісового архіву» від 18.10.2026. Чи є на Google Drive новіші зміни, ще не перевірено.»
That notice disappears after the first successful listing (with Drive sync disabled it stays, since
freshness is never checked). If Google is unreachable, the bundled documents stay fully usable; only
changes made on Drive after the snapshot date are missing until a sync succeeds.

**Documents still loading** is a different state. It applies when listed files have never been processed,
e.g. a file added to Drive after the snapshot whose download Google deferred, or an installation without a
snapshot. Answers then use only the documents ingested so far, and the bot says so. The state is computed
from stored data, with no extra LLM call:
- **an answer was found:** it starts with «⏳ «Лісовий архів» ще завантажується: N із M файлів поки
  недоступні…»;
- **nothing was found:** the reply is «Серед уже оброблених документів відповіді не знайдено… спробуйте
  трохи пізніше», never the plain "not found in the documents".

"Incomplete" means that no successful listing has finished yet, or that listed files have never been
processed (their download was deferred). Files that were processed but **rejected** (shown in
`/pending`), and changed files whose previous version is still active, do not trigger the notice. So a
routine sync on a loaded archive shows nothing.

Without the snapshot (`SNAPSHOT_BOOTSTRAP=false`, a different folder, or an existing database), all files
come from Drive. Google may then temporarily block downloads from one network after a burst, and the bot
retries with bounded backoff. In a clean-start check before the snapshot existed, the archive was complete
about 45 minutes after the start (details in [`docs/VERIFICATION.md`](docs/VERIFICATION.md)).

Data survives `docker compose down` / `up`, restarts and image rebuilds. `docker compose down -v` deletes
the volume, including uploaded documents and user names. The image contains Tesseract (Ukrainian) and the
embedding model, both installed at build time. `.env` is excluded from the image.

---

## Credentials

| Variable | Required | How to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` → name and a username ending in `bot` → copy the token. Without it the container exits with «TELEGRAM_BOT_TOKEN is not set». Never let two processes poll with the same token; use a separate bot for testing. |
| `GROQ_API_KEY` | yes | <https://console.groq.com/keys> → *Create API Key*. |
| `GOOGLE_DRIVE_API_KEY` | for archive sync | See below. Without it the bot runs, but the archive is not synchronised (a warning is logged). |

Never commit `.env`; it is in `.gitignore`. `.env.example` contains only empty placeholders.

### Google Drive (API key)

The archive is read with the **Google Drive API v3 and an API key**. An API key can read files shared as
"Anyone with the link"; no OAuth, no service account, no page scraping.

1. <https://console.cloud.google.com/> → create or select a project (billing not required).
2. *APIs & Services → Library →* **Google Drive API** → **Enable**.
3. *APIs & Services → Credentials → **+ Create credentials** → **API key***. Do not use the "Help me choose"
   wizard: it creates OAuth clients or service accounts.
4. Recommended: *API restrictions → Restrict key →* **Google Drive API**.
5. Put it into `.env` as `GOOGLE_DRIVE_API_KEY=...`. `GOOGLE_DRIVE_FOLDER_ID` defaults to the original
   archive; the folder must stay shared as *Anyone with the link → Viewer*.

> **Setup requirement:** reviewers need their own Google Cloud API key, created as above, or the developer
> must supply one. Whether reviewers are willing to create a key has **not** been confirmed.

### Groq models

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | writes the answers |
| `GROQ_ROUTER_MODEL` | `openai/gpt-oss-20b` | understands the question and classifies unclear document locations |
| `GROQ_FALLBACK_MODELS` | `qwen/qwen3.8-27b,openai/gpt-oss-20b` | used while a model is rate-limited; `none` disables |
| `GROQ_REASONING_EFFORT` / `GROQ_ANSWER_REASONING_EFFORT` | `low` / `medium` | gpt-oss reasoning effort for routing / answers |
| `GROQ_FALLBACK_WAIT_SECONDS` | `2` | longest wait on a limited model while another one is available |
| `REQUEST_BUDGET_SECONDS` | `45` | time budget per question, including retries and rate-limit waits |

Defaults were chosen from Groq's free-tier model list and confirmed available with real keys on 17–18 Oct
2026. **Rate limits differ by account, plan and model and can change.** See
<https://console.groq.com/docs/rate-limits>. As dated observations for our free-tier keys:

- 8,000 tokens per minute and 1,000 requests per day per model;
- `gpt-oss-120b` reached a 200,000 tokens-per-day limit.

One question used roughly 1K router tokens plus 4–6K answer tokens, so a daily quota of that size covers
very roughly 30–50 answers per model. That figure depends on the questions, the documents retrieved and
the corrections needed. When a model is rate-limited, the fallback models are used automatically.
**Sustained load may exceed the capacity of the free tier.** At startup the bot logs an error if a
configured model is not available to the key. Groq has no embedding endpoint; embeddings are computed
locally.

Other settings (sync interval, download pause and backoff, deletion confirmation threshold, upload and PDF
limits, OCR confidence, prompt budget, timezone) are listed with comments in
[`.env.example`](.env.example).

---

## Supported formats and OCR

- **Accepted:** `.pdf`, `.docx`, `.txt` (UTF-8 or CP1251); on Drive also Google Docs (exported as text).
- **Not accepted:** legacy `.doc`, photos and images, other formats. Telegram uploaders get a message;
  unsupported Drive files are listed in `/pending`.
- **OCR:** PDF pages without a usable text layer are rendered at 300 dpi and read with Tesseract (`ukr`).
  - Several preprocessing variants are OCRed. The text comes from the most confident variant, and each
    number is decided by a confidence-weighted vote across variants.
  - Pages below `OCR_MIN_CONFIDENCE` are rejected, so the document is not used.
  - OCR can still misread text.
- **Unusable files** are not used:
  - Telegram: the uploader is told why;
  - Drive: unreadable, empty, password-protected, too many pages, failed OCR, or unsupported format →
    listed in `/pending` with the reason;
  - Drive files larger than the download limit (25 MB) are rejected and listed in `/pending`; they are
    retried when the file changes.
  - **Download-access failures** (permission, credential or not-found errors, recognised from the response)
    are also listed in `/pending`, but rechecked automatically at bounded intervals (1 h, 2 h, 4 h … up to
    24 h), even if the file does not change.
  - Temporary failures (Google's anti-automation page, Drive rate-limit reasons, 429, 5xx, network) leave
    the file waiting and are retried with backoff.
  - The previous valid version stays active in every case.

---

## Using the bot

Questions and uploads need no commands.

**Questions:**

- **Examples:**
  - «Скільки внесок під дубом і до якого числа?»
  - «Біля греблі мисливці! Як кричати?»
  - «Що їсти в листяному лісі?»
  - «Порівняй внески під дубом і у соснах»
- **Follow-ups:** «і до якого числа?», «а кому віддавати?», «а у соснах?». A message that only names a new
  place keeps the latest requested detail.
- **Circumstances:**
  - «я дорослий борсук» or «зараз зима» is remembered until changed or `/reset`;
  - a season named inside a question applies to that question and its follow-ups only;
  - hypothetical wording («якби…», «уявімо…») is not stored.
- **Missing place:** if one is needed, the bot asks with buttons (8 mittens + «Увесь ліс").
- **Sources:** answers end with **Джерела**, built from stored records:
  - Drive: title, «Лісовий архів», file link, place;
  - Telegram: title, the uploader's real name, Kyiv receipt time, place, and a 📎 button for the file
    («Документ відкликано» after withdrawal).

**Uploading (PDF, DOCX or TXT as a file):**

1. First upload: the bot asks for your **real name**; the upload waits. The name is stored against your
   Telegram user ID. Questions never require a name.
2. Text is extracted, with OCR for scans. Unreadable files are reported and not used.
3. The place is taken from the title or an audience sentence. If it is unclear, you are asked (several
   mittens or regions, or «Увесь ліс"). The document is not used until you answer.
4. If it is unclear whether the upload replaces an existing document, you are asked «Замінює» / «Окремий
   документ».

| Telegram command | What it does |
|---|---|
| `/start`, `/help` | purpose, examples, mitten list |
| `/reset` | clears conversation, place and circumstances (documents and name are kept) |
| `/pending` | Drive documents needing clarification or unreadable: title, link, reason (paginated) |
| `/my_documents` | your uploads with status; «Відкликати» → confirmation → withdrawn (paginated) |

**Command-line tools** (`docker compose run --rm bot python -m forest_bot <command>`, no Telegram):

| Command | Purpose |
|---|---|
| `sync` | one Drive synchronisation with a per-file report |
| `status` / `docs` | document counts / every version with status, scope, version time |
| `pending` | the text `/pending` would show |
| `search "query" <mitten-code>` | retrieval only (no LLM) |
| `ask "question"` | one answer (uses Groq) |

`scripts/drive_test.sh` runs these against a separate Drive test folder in an isolated Compose project.
It refuses the original archive's folder ID.

---

## How it works

```
Telegram (long polling) ─► Conversation (dialogue state, context, uploads; SQLite)
          │                                   │
          ▼                                   ▼
  QAService: router LLM + deterministic   Library: extraction/OCR, scope detection,
  place/context detection                 versioning, withdrawal, removal
          │                                   ▲
          ▼                                   │
  Retriever: active chunks only,          DriveSync (background job: startup + every N min;
  geography filter, BM25 + embeddings     Drive API v3 + API key)
          │
          ▼
  answer LLM (Groq) → best-effort answer checks → sources rendered from DB records
```

| Concern | Choice |
|---|---|
| Storage | SQLite (WAL) + original files on the `/data` volume |
| Telegram | python-telegram-bot 22, long polling, private chats |
| Extraction | pypdf, python-docx, UTF-8/CP1251 text, Tesseract OCR |
| Retrieval | geography filter → BM25 (light Ukrainian stemming) + fastembed `paraphrase-multilingual-MiniLM-L12-v2` |
| LLM | Groq: `gpt-oss-120b` (answers), `gpt-oss-20b` (routing), fallbacks |

### Geography and priority

- The mitten → region mapping is in `forest_bot/geography.py`, separate from documents. «Під вербою»
  belongs to «Листяний ліс» and «Біля річки»; «На узліссі» to none.
- Documents and individual **passages** have scopes; a section headed with a place gets that place's
  scope.
- For a mitten, only passages for that mitten, its regions or the whole forest are offered to the model.
  This filter is enforced in code.
- **Priority:** on a conflicting point, the more specific advice applies and the broader one is said not to
  apply there. Complementary broader advice is still given. A mitten document and a broader document about
  the same subject (word overlap) are flagged to the model as a potential conflict. Equal-specificity
  conflicts are shown with both sources.

### Document lifecycle (enforced in code)

| Status | Used in answers | Meaning |
|---|---|---|
| `awaiting_name` / `processing` | no | Telegram upload waiting for the name / being processed |
| `pending_location` | no | place unclear (Telegram: asking the sender; Drive: in `/pending`) |
| `pending_replacement` | no | Telegram: waiting for «Замінює / Окремий» |
| `failed` | no | unsupported or unreadable (Drive: in `/pending` with reason) |
| `active` | **yes** | current version |
| `superseded` | no | replaced by a newer version of the same logical document |
| `withdrawn` | no | withdrawn by its uploader (Telegram user ID checked) |
| `removed` | no | file confirmed deleted from the Drive archive |
| `duplicate` | no | identical content already known |

- **Version time:** Drive `modifiedTime` or Telegram receipt time, never download time (UTC, shown in
  `TIMEZONE`).
- **Replacement:** a newer version of the same Drive file; or the same title and scope from any source
  or uploader; or an explicit update statement naming an existing title. The new version must be
  **later** in each case.
- **Ambiguous identity:** Telegram uploads get a question; on Drive both documents stay active.
- **Not a replacement:** same topic and place alone. Identical content creates no new version.
- **No automatic restore:** withdrawal or removal never reactivates older versions.
- **History:** every document keeps a decision history.

### Drive synchronisation

1. List the folder recursively, all pages. **A listing error, or a page request that fails before
   pagination finishes, aborts the sync without changes.**
2. Skip unchanged files (same `modifiedTime` and checksum).
3. Download new or changed files through the official API:
   - `files.get?alt=media`, or `files/{id}/export` for Google Docs, with the API key;
   - transient 429/5xx: at most 4 attempts. A `Retry-After` up to 30 s is waited in full; a longer one
     defers the request, and no sync contacts Google before that time;
   - errors are classified from the response, not by status code alone:
     - Google's HTML anti-automation page and Drive rate-limit reasons are **temporary**, even when sent
       as HTTP 403. They stop downloads for that run, keep completed downloads, and retry with bounded
       exponential backoff (5, 10, 20, 40 min, then hourly; reset after a clean sync);
     - permission, credential or not-found errors are **access** failures: the file is rejected for now
       and rechecked at 1 h, 2 h … 24 h intervals.
4. Files missing from a successful listing are marked `removed`. If more than
   `DRIVE_MAX_DELETE_FRACTION` of known files (and more than 3 files) are missing at once:
   - the removal is first confirmed by a second listing; files missing from both are removed, whatever
     their number, and files that reappear are kept;
   - if the confirmation listing fails, nothing is removed, and the check repeats on the next sync.
5. Pending or unreadable files are reprocessed when their `modifiedTime` changes; the previous valid
   version stays active meanwhile.

Documents imported from the snapshot are ordinary Drive records (same file IDs, `modifiedTime`, checksum
and links), so all of the above applies to them unchanged.

### Bundled archive snapshot

`snapshot/` holds a copy of the original «Лісовий архів», taken on **18 Oct 2026**:
- `files/<Drive file ID>.<ext>`: the 78 original files, byte for byte;
- `manifest.json`: folder ID, snapshot date, and per file the Drive file ID, name, MIME type, original
  `modifiedTime`, Drive `md5Checksum`, size, link, SHA-256 of the bundled bytes, and the extracted / OCR
  text with its method.

It contains no Telegram uploads, user names, conversations, credentials or database. The same files are
used as the test fixtures.

**When it is imported.** Only when all of these hold; otherwise it is ignored with a log line:
- `GOOGLE_DRIVE_FOLDER_ID` equals the manifest's folder (test and custom folders are never seeded);
- the database has no documents and no Drive records (existing installations keep their state);
- no import was recorded before (restarts never re-import, so superseded, withdrawn or removed documents
  are never restored).

Each file goes through the normal Drive ingestion path with its original `modifiedTime` as the version
time; the location comes from the document text, without the LLM. A file whose bytes do not match the
manifest's SHA-256 is skipped and later downloaded by the sync. `SNAPSHOT_BOOTSTRAP=false` disables the
import.

**Refreshing it** (from the Drive API, never from a runtime database):

```bash
docker compose run --rm -v "$PWD/snapshot":/app/snapshot bot python -m forest_bot snapshot-build
```

The command lists the configured folder and reuses a bundled file only if its MD5 equals Drive's
`md5Checksum`; other files are downloaded (2 s apart) and their text extracted again. Files that are no
longer on Drive are dropped. `--seed-dir DIR` reuses local copies that pass the same check (the first
snapshot was built from the Drive folder download, with every file verified this way). If Google blocks
downloads, the command stops; files fetched so far are kept and reused on the next run.

### Answer safeguards

These fall into three categories with different strength.

**Enforced in code (deterministic):**
- which documents are eligible (status and geography);
- ownership checks for withdrawal;
- version timestamps;
- source metadata: titles, links, uploader names and times always come from stored records, never from
  model output.

**Intended model behaviour (prompt rules):**
- answer only from the provided documents, in Ukrainian;
- treat document text as data, not instructions;
- priority and conflict handling;
- complete but focused answers, covering every part of a multi-part question;
- measures and units exactly as in the source, no added how-to steps;
- an override explained as the priority of the more specific place, without invented distinctions;
- no unsupported comparisons.

The model is instructed accordingly but can deviate. In particular, text inside documents cannot be
guaranteed never to influence it.

**Best-effort answer checks (heuristics):** applied after the model answers. They use lexical evidence
(word overlap and patterns), not semantic verification, so they reduce rather than eliminate unsupported
statements.

- **Deterministic repairs:**
  - remove claims about "other documents";
  - restore the source's recipient form («Синиці в кошик»);
  - remove season remarks unrelated to any document.
- **Conflict hints:** sentences of a more specific and a broader document about the same subject are
  marked in the prompt, including short ones with different months or a prohibition vs a conditional
  permission.
- **Detectors for typical errors.** One correction request (which never waits for a rate limit), then
  removal or neutral replacement of sentences that still fail; a changed measure or a broader month is
  replaced by the cited source sentence instead of being deleted. After a removal, a dangling opening
  connective («Тому», «Отже») is dropped; this is cosmetic and does not guarantee a coherent answer.
  - absence stated as fact;
  - invented ownership;
  - dropped conditions such as «до року»;
  - a rule named for one animal restated without it or turned into a prohibition;
  - obligations inferred from silence;
  - numbers found in no source;
  - a measure turned into another unit or count, or declared unspecified although the source gives it;
  - a broader document's month where a more specific document sets another;
  - a contrast between two forms of the same word («брід» / «вбрід») that no document makes;
  - unsupported "cheaper" conclusions;
  - an ignored seasonal rule.
- **Citation check per clause:**
  - citations whose document shares none of the clause's distinctive words are dropped;
  - a document sharing at least two otherwise uncited distinctive words is added.

  This catches clear cross-document misattributions; it does not prove that each clause is semantically
  supported.

**Failures:** each question has a time budget. If all models are rate-limited, the user is told to try
again shortly; API errors give a "technical problem" message. Neither is presented as "not found in the
documents".

---

## Testing

```bash
docker compose run --rm bot python -m pytest -q             # deterministic tests, incl. real OCR
pip install -r requirements-dev.txt && python -m pytest -q  # locally; the OCR test needs tesseract
```

The deterministic tests need no network or credentials. They run on the real archive (the bundled
`snapshot/`, files and extracted texts), with a fake Drive folder, a fake LLM and a mocked HTTP layer.

| Area | Test files |
|---|---|
| geography, place detection, document and passage scope | `test_geography.py`, `test_scope.py` |
| extraction and OCR (real scanned PDF in Docker) | `test_extraction.py` |
| lifecycle, versioning, replacement, withdrawal | `test_versioning.py`, `test_upload_lifecycle.py`, `test_conversation.py` |
| Drive sync: listing failures, deletion confirmation, throttling, Retry-After, endpoints | `test_drive_sync.py` |
| archive readiness notices, download error classification | `test_archive_readiness.py` |
| bundled snapshot: import guards, no OCR, sync on top, restarts, rebuild | `test_snapshot.py` |
| retrieval and priority | `test_retrieval.py` |
| routing, follow-ups, dialogue context, customer scenarios | `test_qa.py`, `test_scenarios.py`, `test_dialogue.py` |
| answer checks and citations | `test_comparisons.py`, `test_citations.py`, `test_answer_polish.py` |
| Telegram HTML payload: entities, bold, angle brackets | `test_rendering.py` |
| target vs cited places, same-place hints, month repair in comparisons | `test_place_reference.py` |
| requested aspects, month citations, claim-level measure check | `test_scope_attribution.py` |
| replays of observed answer errors (fake model outputs) | `test_answer_accuracy.py` |
| Groq client, fallbacks, rate limits, time budget | `test_llm.py`, `test_rate_limits.py` |

Optional live tests (`-m live`, need keys, consume Groq quota): `tests/live/test_live_drive.py`,
`tests/live/test_live_groq.py`.

---

## Verification summary

Dated details are in [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

| Area | Offline tests | Live evidence |
|---|---|---|
| Drive listing, download, extraction of the original archive | ✅ | ✅ scripted live test (3/3) |
| Drive add / update / unclear place / clarification / listing failure / removal (A–F) | ✅ | ✅ scripted, separate test folder |
| Mass-deletion confirmation, failure mid-pagination, single-file download failure, Retry-After | ✅ | — offline only |
| Google download block and backoff | ✅ | ✅ observed twice; completed after backoff |
| Clean start: build and missing-token validation | — | ✅ |
| Clean start without snapshot: full archive ingestion into an empty database, persistence after restart | ✅ | ✅ (completed ≈45 min after start, after a Google block) |
| Snapshot start: empty volume, no network → 78 documents ready, "freshness unverified" state; restart skips the import | ✅ | ✅ Docker, `--network none`: local archive readiness ≈7–13 s after process start (excl. build and Telegram connection; one machine) |
| Snapshot start, then a real Drive listing: unchanged files not downloaded | ✅ | ✅ one live listing: 78 unchanged, 0 downloads |
| Snapshot records updated / removed by sync; custom folders not seeded; restart does not restore old versions | ✅ | — offline only |
| Clean start: Telegram-connected startup on an empty database with the current release | — | ⏳ **not tested** (needs a separate test bot) |
| Readiness notice while the initial archive is incomplete; oversized files and download-access failures shown in `/pending` (access rechecked automatically); anti-automation 403 kept as a temporary block | ✅ | — offline only (added after the clean-start check) |
| Telegram upload lifecycle across users: name collection, place clarification, replacement by another user, withdrawal, per-user lists, regional fallback, persistence after restart | ✅ | ✅ manual tests by the developer (logs corroborate replacements and withdrawals) |
| Version ordering across Drive and Telegram (`modifiedTime` vs receipt time) | ✅ | — offline only |
| Questions, follow-ups, context, comparisons, missing information | ✅ (fake LLM) | ✅ real models and manual Telegram tests; **full live suite not re-run after the latest answer-check changes** |
| Fixes for the 18 Oct manual findings (ford, insulation, head/treasurer) | ✅ replays of observed outputs with a fake model | ✅ all three passed a manual retest (ford after a further fix); replays do not establish live answer quality |
| Fixes for the 18 Oct evening batch (false premise «40», citation, applicability) | ✅ replays of reconstructed outputs | ✅ false premise, citation and the riverside suggestion passed a manual retest; materials comparison passed a manual retest after the citation, measure and aspect fixes |
| Rate-limit fallback | ✅ | ✅ observed when a daily quota ran out |

**Observed latency** (18 Oct, this machine and network, free-tier keys): 1.3–2.8 s per question when
not rate-limited, about 5 s when a correction call was made, and longer waits under rate limiting. These
are observations, not guarantees.

---

## Known limitations

- **Fresh installation:**
  - the archive is available immediately from the snapshot of 18 Oct 2026. Changes made on Drive after
    that date arrive only after a successful sync; during a Google outage or download block they stay
    unavailable, and the bot says that freshness has not been checked;
  - files added or changed on Drive after the snapshot are downloaded normally, so they can still be
    delayed by Google's download protection (not a documented quota);
  - the "still loading" notice is based on stored state: it covers files that are listed but not yet
    processed, not changed versions of files that are already loaded;
  - the snapshot only grows stale; refreshing it is a manual command and a new commit;
  - Telegram-connected startup on an empty database with the current release was not tested.
- **Drive listings:**
  - listing errors and unfinished pagination are detected and cause no deletions;
  - a successful listing that silently omits files cannot be detected in general. Large drops are
    confirmed by a second listing, but small omissions are treated as removals, and two consistent
    listings cannot guarantee that Drive returned every file.
- **Answer checks are heuristics.** Wording, completeness and conflict judgement come from the model and
  vary between runs. Checks use word overlap and patterns, not semantic verification. Same-subject conflict
  hints rely on word overlap, so differently worded conflicts rely on the prompt rules.
- **Place routing:** a part of the forest named as the source of a suggestion («біля бобрової греблі … як
  радять для хвойного лісу») is recognised from a fixed set of advising words and treated as a reference,
  not as a place asked about. Other phrasings («я чув, що в хвойному лісі…») are still treated as a
  comparison of places.
- **Dialogue context** recognises the four seasons, current-season markers, hypothetical wording, and the
  archive's animals in first-person statements; other phrasings may be missed.
- **«Під вербою»'s two regions:** no priority between them was agreed. As the implementation default, the
  equal-specificity conflict rule applies, and conflicting advice from both regions is shown with sources.
- **Identity is conservative:** different titles mean a replacement only with an explicit update statement
  or the uploader's confirmation.
- **Open uploads (customer decision):** self-reported names; any user may upload, and an upload may replace
  a matching document from any source.
- **Groq:** limits depend on account, plan and model. The free-tier capacity we observed may not be enough
  for sustained load.
- **OCR** can misread scanned text.
- **Formats and Telegram:** PDF, DOCX, TXT (plus Google Docs on Drive) only; files up to 20 MB; private
  chats only. An upload being processed during a restart is marked `failed` and must be resent.
- **Hosting:** the bot must run continuously; on a laptop, sleep pauses Docker and the bot.

## Project layout

```
forest_bot/
  __main__.py      entry point: run | sync | status | docs | pending | search | ask | snapshot-build
  app.py           service wiring and startup checks
  config.py        environment configuration
  geography.py     mitten/region model, place detection
  dialogue.py      dialogue context: season, identity, hypotheticals
  extraction.py    PDF/DOCX/TXT + OCR
  scope.py         document and passage scope; LLM scope validation
  library.py       lifecycle: ingestion, versioning, withdrawal, removal
  drive.py         Drive API client and synchronisation
  snapshot.py      bundled archive snapshot: guarded import and rebuild
  retrieval.py     geography-aware hybrid retrieval
  embeddings.py    local fastembed model
  textnorm.py      tokenizer and light Ukrainian stemmer
  llm.py           Groq client (fallbacks, rate limits, time budget)
  qa.py            routing, answer prompt, answer checks, rendering
  timing.py        per-request latency measurement
  conversation.py  dialogue logic (transport-independent)
  telegram_bot.py  Telegram adapter
  messages.py      Ukrainian texts
  storage.py       SQLite schema and repository
snapshot/               bundled «Лісовий архів» snapshot (manifest.json + original files)
scripts/drive_test.sh   isolated live Drive sync checks against a test folder
tests/                  deterministic tests + tests/live (optional)
docs/                   REQUIREMENTS.md, PLAN.md, VERIFICATION.md
```
