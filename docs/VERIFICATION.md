# Verification record

Dated evidence for what was checked, how, and with which limits. The README has a short summary. Three
kinds of evidence are distinguished:

- **Automated offline:** deterministic tests, no network or credentials (`python -m pytest`).
- **Live automated / scripted:** real Google Drive or Groq, run by the developer with a script or test.
- **Manual live:** the developer used the real Telegram bot. These results are as reported by the
  developer; where noted, application logs corroborate them.

All times are UTC.

---

## 1. Offline test suite

- Final run (18 Oct 2026): 244 tests pass inside the Docker image (Tesseract included); locally 243 pass
  and 1 is skipped (the OCR test, which needs Tesseract).
- They run on the real archive texts (`tests/fixtures/archive_texts.json`, extracted from all 78 files by
  this code, including OCR), a fake Drive folder, a fake LLM that records prompts, and a mocked HTTP layer
  for the real Groq and Drive clients.
- Coverage per file is listed in the README (*Testing*).

## 2. Google Drive — original archive (17 Oct 2026)

`tests/live/test_live_drive.py`, 3/3 passed:

- paginated listing (10-item pages) of all 78 files, with `modifiedTime`;
- download and extraction of one DOCX, one PDF and one TXT;
- full sync into a temporary store (78 added, 0 errors), then an idempotent re-sync.

## 3. Google Drive — separate test folder, steps A–F (18 Oct 2026)

`scripts/drive_test.sh`, isolated Compose project `forest-drive-test` (own volume, no Telegram). The
original archive was not modified, and the production database was not used.

| Step | Developer action in Drive | Observed result |
|---|---|---|
| A | new Google Doc «Тестова толока» for «Під дубом» | `added=1`; active for «Під дубом»; version time = Drive `modifiedTime`; not retrieved for «У соснах»; re-sync `unchanged=1` |
| B | same doc: «листопада» → «грудня» | `updated=1`; v2 active, v1 `superseded`; retrieval returns «грудня» only |
| C | new Google Doc «Нотатки про ліхтар» without a place | `pending=1`; `/pending` text lists title, file link and reason; retrieved for none of 8 mittens or the whole forest |
| D | same doc: added «Для мешканців рукавички «Під вербою».» | v2 active for «Під вербою» (scope evidence = that sentence); `/pending` empty; not retrieved for other mittens |
| E | none; sync run with an invalid API key | Google rejected the key; sync recorded as failed; before/after status of every record identical |
| F | «Тестова толока» moved to trash | `removed=1`; v2 `removed`, v1 stays `superseded` (not reactivated); lantern doc unaffected; second sync idempotent |

Notes:

- Row C's earlier pending version predates a later bookkeeping fix, so it is still recorded as
  `pending_location` in that test database. It is not listed by `/pending` and never used.
- `/pending` was checked as the exact text the command produces (CLI), not in a Telegram chat.

## 4. Drive download endpoint and Google's download block (18 Oct 2026)

- **Code:**
  - PDF/DOCX/TXT: `GET https://www.googleapis.com/drive/v3/files/{id}?alt=media&supportsAllDrives=true&key=<REDACTED>`;
  - Google Docs: `GET …/files/{id}/export?mimeType=text/plain&key=<REDACTED>`;
  - no browser-style URLs.
- **On the wire:** one request each for a PDF, a DOCX and a TXT from the original archive, redirects not
  followed. All three returned 403 directly from `www.googleapis.com` with Google's HTML page "your computer
  or network may be sending automated queries" (not a Drive API JSON quota error). No redirect. Listing
  with the same key at the same moment returned 200.
- **Occurrences on this network:**
  - 17 Oct, at the production bot's first start;
  - 18 Oct, during the clean-start check.

  Both times after a burst of about 40 file downloads within minutes. This is an observation, not a
  documented Google threshold.

## 5. Clean startup (18 Oct 2026)

Isolated Compose project `forest-clean`: a fresh copy of the tracked files, empty volumes, and `.env` built
from `.env.example` with only the documented variables. `TELEGRAM_BOT_TOKEN` was left empty on purpose, so
that no second process polled with the production token.

| Check | Result |
|---|---|
| Build and missing-token validation | ✅ passed. No-cache build ≈1 min; container stopped with «Configuration error: TELEGRAM_BOT_TOKEN is not set». `.env` is not in the image. |
| Full archive ingestion into an empty database | ✅ passed, after a temporary Google download block. 12:25 sync: 41 files, then the block, remaining files deferred (not failed). Diagnostic requests at ~12:44 were still blocked. One sync at 13:06, after the backoff interval: 37 added, 41 unchanged, 0 errors. All 78 active (21 TXT, 19 DOCX, 19 PDF text layer, 19 PDF via OCR), ≈45 min after the start. |
| Persistence across restarts | ✅ passed. New containers saw the same documents; re-sync `unchanged=78`, nothing re-downloaded. |
| Telegram-connected startup on an empty database with the current release | ⏳ not tested (needs a separate test bot token). For context only: the production bot's first start on 17 Oct (earlier code) was Telegram-connected, began with 0 documents, hit the block, and had all 78 documents by 20:25, ≈1.5 h later, partly while the host was asleep. |

## 6. Production bot (18 Oct 2026)

Last recreated at 14:22, after the deletion-confirmation, backoff and Retry-After changes (12:47) and the
readiness-notice and download-error changes, keeping its volume:

- the running container uses the current image ID, and the new code is present inside it;
- volume `forest-assistant_forest-data` mounted at `/data`;
- 78 archive documents active; startup sync `unchanged=78`;
- exactly one container runs the bot command; no Telegram "Conflict / terminated by other getUpdates"
  error in the log;
- the upload history was identical before and after (6 Telegram documents with their statuses; 2 stored
  user names), and the in-place migration added the access-recheck columns;
- one later review fix (block detection no longer accepts generic HTML "sorry" pages) is in the commit
  but **not** in the running container, which keeps the 14:22 image until it is recreated.

## 7. Telegram — manual live tests (17–18 Oct 2026, developer)

Reported by the developer using the real bot; offline tests cover the same flows.

- **Questions:**
  - the four customer examples;
  - follow-ups («і до якого числа?», «а кому віддавати?», «а у соснах?»);
  - season and identity context (winter → «А влітку?», «я дорослий борсук», «А для зайчика?»);
  - comparisons; off-topic; missing information («пароль від Wi-Fi»).
- **Multi-user upload lifecycle:**
  - first-upload name collection;
  - geographic clarification;
  - replacement of one user's document by another user;
  - withdrawal;
  - ownership-separated `/my_documents` lists;
  - regional fallback (a «Листяний ліс» document answering for «Під дубом» while «Під вербою» used its
    own);
  - persistence after a restart.
- **Log corroboration (application log):**
  - replacement events («Document 80 «Тестова комора» replaced 79»);
  - withdrawals by two different Telegram user IDs;
  - per-request routing and retrieval lines for the questions above.
- **Not covered manually:** the order of versions across Drive and Telegram (Drive `modifiedTime` vs
  Telegram receipt time). That is covered offline only (`test_versioning.py`).

## 8. Groq (17–18 Oct 2026)

- Keys and all configured models (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) were
  confirmed available by the models endpoint.
- **Free-tier limits**, as dated observations for these accounts and models (see
  <https://console.groq.com/docs/rate-limits> for current values):
  - response headers for the key used on 18 Oct: 8,000 tokens/minute and 1,000 requests/day for each of
    the three models;
  - the daily limit hit with the 17 Oct key: Groq's error for `gpt-oss-120b` stated
    "tokens per day (TPD): Limit 200000".
- **Live answer runs:** the customer scenarios, follow-ups, place switches, the comparison and
  eligibility questions were run against the real models during development.
  - **Findings that led to changes:** omitted details, an invented conflict, broken JSON output, an
    unsupported "cheaper" conclusion, and an invented prohibition.
  - **Last runs:** the four customer scenarios passed (with run-to-run variance: occasionally a detail was
    omitted); the comparison passed twice; the eligibility question passed on the main model.
  - **Not re-run:** the full live suite was not re-run after the latest answer-safeguard changes.
- **Fallback:** when `gpt-oss-120b`'s daily quota was exhausted, answers continued from the fallback
  models; their answers were less complete in testing.

## 9. Initial-archive readiness notice (18 Oct 2026, offline)

Added after the clean-start check showed that partial ingestion produced plain "not found" answers.
Covered by `tests/test_archive_readiness.py` (offline only; no live run):

- status before the first listing, during a block, and after completion;
- routine sync of an already loaded archive shows no notice;
- rejected files (unsupported, oversized, permanent HTTP error) are not counted as loading, while a
  transient error is;
- the answer notice, the "not found among processed documents" reply, and the unknown-total wording;
- the observed HTML anti-automation 403 page and a Drive rate-limit 403 both keep the file waiting
  (not rejected);
- a generic HTML error page («Sorry, the file … does not exist») is not mistaken for the block and is
  handled as an access failure;
- an access 403 is rejected visibly, with the previous version kept active and no retry before the recheck
  time. The recheck happens without a file change, the backoff doubles while still denied, and
  recovery of an unchanged file clears `/pending` without creating a new version;
- older databases get the new columns by an in-place migration.

## 10. Latency (18 Oct 2026)

Per-request `Timing:` log lines (no message text or keys) on this machine and network, free-tier keys:

- **Not rate-limited:** 1.3–2.8 s per question in a paced benchmark (router 0.2–0.8 s, retrieval ≈0.1 s,
  answer 0.9–2 s). In manual use, 2.0–3.6 s typical, and about 5 s when a correction call was made.
- **Rate-limited:** before the fallback change, requests waited 23–31 s on a per-minute limit while
  another model was available. After the change, the same benchmark request took 2.6 s. Waits still
  occurred when every model was limited, and once a correction call waited 29 s; corrections no longer
  wait for rate limits.
- These are observations, not guarantees.
