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

- Latest run (19 Oct 2026, after the fixes in sections 12 and 13): 327 tests pass inside the Docker image
  (Tesseract included); locally 326 pass and 1 is skipped (the OCR test, which needs Tesseract).
- They run on the real archive: the bundled `snapshot/` (original files and the texts extracted from all
  78 files by this code, including OCR), a fake Drive folder, a fake LLM that records prompts, and a mocked
  HTTP layer for the real Groq and Drive clients. The snapshot replaced the earlier fixtures
  (`tests/fixtures/`); before removing them, all 78 texts and the 4 sample files were confirmed identical
  to their snapshot copies.
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

## 5. Clean startup without the snapshot (18 Oct 2026)

This check predates the bundled snapshot (section 11); every file came from Drive.

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

## 11. Bundled archive snapshot (18 Oct 2026)

**Building.** `python -m forest_bot snapshot-build --seed-dir <Drive folder download>` in Docker: one live
listing of the original folder (78 files); every local copy matched Drive's `md5Checksum`, so nothing was
downloaded; text was extracted by this code (19 DOCX, 21 TXT, 19 PDF text layer, 19 PDF OCR). All 78
texts are identical to the earlier test fixtures. Snapshot: 78 files, 14 MB, `created_at`
2026-10-18T15:44:49Z.

**Offline tests** (`tests/test_snapshot.py`, 12 tests):
- manifest provenance: IDs, names, MIME types, `modifiedTime`, link, SHA-256 and MD5 of every file; no
  database in the snapshot;
- fresh import: 78 active Drive documents with the original `modifiedTime` and links; extraction/OCR and
  the LLM scope classifier are never called (both fail the test if used);
- Drive listing failure after the import: all 78 stay active; answers carry the "local copy, freshness
  unverified" notice, and "not found" says the same instead of claiming the whole archive was searched;
- unchanged listing: 0 downloads, notice gone; a modified file: 1 download, old version `superseded`, new
  version active with the new time; a deleted file: `removed`;
- a corrupted bundled file is skipped and later downloaded by the sync;
- a different (test/custom) folder: nothing imported; existing databases: nothing imported;
- two restarts after an update, a removal and a withdrawn upload: no re-import, all statuses unchanged;
- rebuild: reuses verified files, downloads only MD5 mismatches, re-extracts only changed bytes, drops
  files no longer on Drive.

**Docker, fresh volume, `--network none`** (startup path `build_services` + `startup_checks`, i.e.
everything before Telegram polling; Telegram itself was not started, to avoid a second poller with the
production token):

| Run | Ready (78 active documents, index built) | State |
|---|---|---|
| fresh volume, first run | 12.4 s after process start | local snapshot, freshness unverified; not "loading" |
| fresh volume, second run | 7.3 s (≈2 s of it loading the embedding model) | same |
| restart on the same volume | 2.1 s, import skipped ("already bootstrapped") | same |

These are **local archive readiness** times after process start, on one machine: they exclude the image
build (and the embedding model download it includes) and the Telegram connection, and they are not a
startup guarantee for other machines.

The sync attempt without network failed after 15 s (DNS failure, bounded retries); all 78 documents stayed
active and the state did not change.

**Live Drive listing on top of the snapshot** (same test volume, network on): `listed=78 unchanged=78`,
0 downloads, and the state switched to verified.

**Not verified live:** sync updates and removals of snapshot-backed records (offline only), and a
Telegram-connected start with the snapshot (needs a separate test bot). The production bot was not
changed: it runs on its existing database, where the import is skipped by design.

## 12. Answer errors from manual tests (18 Oct 2026)

Three answers from the developer's manual tests were wrong. Investigated from the production logs (models,
fallbacks, correction calls, safeguard actions) and by rebuilding the real prompts on production data
without LLM calls. In all three cases every needed fact was retrieved and was in the prompt. The logs do
not record answer text, so only the logged sentences are quoted here.

| Question | Models | Diagnosis |
|---|---|---|
| Ford at the beaver dam: correct «no», but an invented «брід vs вбрід» distinction | first answer gpt-oss-120b; correction qwen/qwen3.8-27b (fallback; 120b rate-limited) | Post-processing false positive: the qualifier check did not accept «лише за умов…» as restating «тільки тоді, коли…» and requested a correction. The final answer came from that correction call; **which call originated the distinction is not confirmed**. No conflict hint was generated for the local ban vs the regional conditional permission. |
| Insulation «У соснах»: «три шари», thickness "not specified", added «рівномірно», only «Календар сезонів» cited | first answer gpt-oss-20b (fallback); correction gpt-oss-120b | **Generation error, compounded by post-processing.** The model misread «шаром у три лапи», declared the thickness unspecified and added an application instruction. The ownership check then misread the sentence-initial noun «Товщина» as a possessive adjective; the sentence still failed after the correction and was deleted, which removed the only citation of the coniferous document. No October-vs-November conflict hint was generated. |
| Head and treasurer «Під вербою» omitted | first answer qwen/qwen3.8-27b (fallback); correction rate-limited on all models | **Caused by post-processing.** The model's sentence «Головою ОСМД … є Качка, а скарбником — Жабка [1]» was correct; the ownership check misread «Головою» as a possessive adjective, and the fallback deleted it. |

**Fixes:** possessive check limited to adjectives formed from a character name in the sources; «лише» /
«тільки» / «лиш» / «виключно» treated as one qualifier; conflict hints for short same-subject sentences
with different months or a prohibition vs a conditional permission («вбрід» ~ «брід»); new checks for
changed or denied measures, broader months overriding a more specific document, and contrasts between
forms of the same word; a failing measure or month is replaced by the cited source sentence; prompt rules
for measures, override explanations and multi-part questions. Across 64 sample prompts (8 mittens × 8
topics) the conflict hints went from 12 to 14: only the real October/November insulation conflicts were
added.

**Tests:** `tests/test_answer_accuracy.py` (15 tests, incl. the retest below) runs the full answer path on the real archive with a
fake model that **replays the observed outputs** (and expected correct ones) and checks the final answer
and its sources. These replays show how retrieval, prompts and post-processing now handle those outputs;
**they do not establish live answer quality**, which depends on the models and varies between runs. The
answers that were correct in the same session (willow stones, hedgehog in November, hazel honey and
raspberries) are replayed as well and pass unchanged.

**Not guarded deterministically:** invented application wording such as «рівномірно по всій поверхні»
(prompt rule only).

**Manual retest (developer, 18 Oct 2026, after deploying `443638b`):** insulation passed (October, moss and
dry needles, «шаром у три лапи», coniferous source); head, treasurer and winter recipient passed. The ford
answer had the correct prohibition and no invented distinction, but was a fragment starting with «Тому, …»
and without the alternative «only via the dam».

**Ford retest diagnosis — caused by post-processing.** From the log: gpt-oss-120b answered (router
gpt-oss-20b). Its first answer cited sources as «【n】»; citations were normalised to «[n]» only when
rendering, after the checks, so the number check read 1, 4 and 3 as numbers found in no document and
flagged three sentences (the local prohibition, the priority explanation, and the rule to cross only by the
dam). The correction was rate-limited on all models, and the fallback deleted the three sentences. Only
the first 120 characters of flagged sentences are logged; **the full intermediate texts were not recorded**.
The same log shows the router setting the season «зима» for this question («вода вже спала»); that only
widened the search query.

**Fixes:** citations are normalised when the answer is parsed, before any check; a season from the router
is ignored unless the message itself names a season or month (a season inherited from the conversation or
established as current, e.g. «Зараз зима.», is kept; tested offline); when the fallback deletes a
sentence, a following sentence that opens with a connective («Тому», «Отже», «Крім того»…) loses it. That
is a **cosmetic cleanup, not a guarantee of semantic coherence**: the remaining text may still refer to
what was deleted. The main fix is that the correct sentences are no longer deleted. Regression tests replay the reconstructed first answer (logged sentence beginnings,
«【n】» form, surviving fragment) and check the final answer: whole, starting with the prohibition, with the
alternative and the priority explanation, local source cited. This replay fails without the citation fix.
The expected answer («Ні. … тільки по греблі … Місцева заборона має пріоритет…») passes unchanged; the
insulation and multi-part replays are unchanged.

**Manual ford retest (developer, 18 Oct 2026, after deploying `fb9184e`): passed.** The answer was complete:
the prohibition, crossing only via the dam, and the geographic priority. Log: answered by gpt-oss-120b
directly, no correction call, no fallback. Remaining source-fidelity details in the model's wording (not
addressed by code, to avoid a heuristic for one phrasing): the regional advice was restated without «вище
греблі», and the guests' rule was widened to "everyone who is not a resident"; the answer was also longer
than the concise local answer the question needs.

**Formatting of that answer:** the copied final paragraph contained literal HTML entities and broken bold
markers. The answer text is not logged and the stored conversation history keeps only its beginning, so
the exact sent text is unknown and the cause is **not determined**. An offline check of the rendering
path found that ordinary model text renders cleanly and that Telegram rejected nothing during the retest,
but that two bot-side paths would show exactly these artefacts in Telegram: bold markers spanning a line
break or left unpaired stay literal, and entities or tags written by the model itself («&quot;», «<b>»)
are escaped again and displayed literally.

**Formatting fix (presentation only).** Entities written by the model are decoded exactly once when the
answer is parsed, before the checks (the offline payload test also showed «&#x2014;» being read by the
number check as 2014 and its sentence deleted, the same class of problem as «【n】»); rendering then
escapes the text for Telegram. Only `<b>`, `<strong>` and `<br>` are interpreted; other angle-bracket text
stays ordinary text. Markdown and HTML bold are paired per line, and an unpaired marker is dropped.
`tests/test_rendering.py` (18 tests) checks the payload passed to `send_message` with a strict model of
Telegram's HTML mode (supported tags only, balanced, entities decoded): hex and decimal numeric entities,
named entities, decoding exactly once, Markdown/HTML/uppercase bold, multiline and unpaired bold, unclosed
tags, literal «<», «>» and «<i>» text, `<br>`, Ukrainian characters, quotes, numbers, citations, and in the
end-to-end payload the source label and `https://` links. Whether the artefacts in the copied ford answer
came from the bot or from copying remains undetermined.

## 13. Manual batch, 18 Oct 2026 evening (deployed `666dce2`)

Investigated from the production log and replayed offline; answer texts are not logged, so replays use
reconstructed outputs and show what the pipeline does with them, not the exact text sent.

**Passed (developer's report):** most of the batch, including the woodpecker «Під ялиною» (half of the
3-cone contribution; the explicit 1.5 was removed by a safeguard false positive, see below — a clarity
loss, not a factual failure).

| Finding | Models | Diagnosis |
|---|---|---|
| «40 жолудів … підтвердь» answered «Це зазначено в документі … [1]» | gpt-oss-20b (fallback), after 35 s of rate-limit waits; no time left for a correction | **The log confirms that the fallback deleted a sentence containing «40»** (number found in no document) and that the shown answer was what remained. Without the full text it **cannot be established whether the original answer correctly rejected 40**. A reconstructed replay demonstrates the failure mechanism: a correct correction («Ні, 4 жолуді …, а не 40») is deleted whole and leaves exactly the reported fragment, which then reads as a confirmation. |
| Hazel February → April: exemption cited to the fees document **and** «Як утеплити рукавичку в листяному лісі» | qwen/qwen3.8-27b (fallback) | **Citation repair added it** (log: «[1] corrected to [1, 2]»). Replay: the clause's distinctive words not covered by the fees document were only «потрібно» and «тих», which the insulation text also contains; two such words met the "two uncited words" threshold. |
| Dam: riverside insulation said to have priority over coniferous advice | qwen/qwen3.8-27b (fallback); correction failed | Place detection treats «біля бобрової греблі … для хвойного лісу» as a comparison and adds the coniferous mittens, so coniferous advice appeared applicable; the model framed it as priority. Also a **post-processing false positive**: «… не належить до хвойного лісу» was deleted as invented ownership («належить»). Routing left unchanged (changing mitten + region detection risks real two-place questions). |
| «Мурашник може кусати» | first answer gpt-oss-120b; correction qwen/qwen3.8-27b, triggered by the same «належить» false positive | The forest-level source itself says «Мурашники кусаються» («Небезпеки хвойного лісу»); the local source says «мурахи кусають». The wording follows a source, but the local, more precise wording is preferable. |
| «шаром у дві лапи» → «дві лапи рукавички»; November inferred from «листя облетіло, снігу ще немає» | gpt-oss-20b (fallback); no check fired | **Generation.** The deciduous document sets the time by these very signs; the forest-wide calendar's November was added. The measure check accepts it (same number and unit). |
| «Якщо у вас більше лісових горіхів…» | not identifiable from the log | **Generation.** The source's «Це зручно для тих, у кого саме горіхів більше» is an explanation, not a condition. |

**Fixes:** a number from the user's question that the answer explicitly rejects («а не 40», «не 40»,
«неправильно») is not an unsupported claim; after a removal, an anaphoric sentence («Це …», «Цей …») whose
antecedent is gone is dropped, and if no sourced statement remains the answer is a neutral «could not
answer reliably» message instead of a bare «Ні.»/«Так.» or fragment; citation repair ignores function
words («потрібно», «тих», «саме»…) when adding a document; halving stated as «половину» allows the derived
half; «належить до <частини лісу>» is geography, not ownership; the prompt says that advice of a part of
the forest the mitten does not belong to «не застосовується» (not "lower priority"), and adds rules for
false premises («Ні, …, а не N») and against inferring months from circumstances or conditions from
explanations.

**Tests** (`tests/test_answer_accuracy.py`, 7 new; each fails without its fix): false-premise correction
kept whole; a confirmed false premise and an invented number never leave a confirmation or fragment; a
bare yes/no is never left; the fee exemption is cited only to the fees document; «1,5 шишки» kept; dam
answer keeps «не належить до хвойного лісу», with the applicability line in the prompt.

**Remaining location-routing limitation:** naming a part of the forest as a proposed source of advice
(«біля бобрової греблі … як радять для хвойного лісу») can be misclassified as a comparison, which adds
that part's mittens to the question. The prompt line on applicability mitigates the effect on the answer
but does not fix the routing.

**Not addressed in code (prompt rules only):** «дві лапи рукавички», the November inference and the
explanation-as-condition wording. **Not verified live:** these fixes have not been retested manually.

**Manual retest (developer, 18 Oct 2026, after deploying `c96df76`):**
- false premise «40 жолудів … підтвердь»: **passed**;
- hazel February → April: exemption and citations **passed** (only the contribution document cited);
- riverside insulation with a coniferous suggestion: practical advice correct, but the **applicability
  explanation failed** — the answer still said riverside advice «має пріоритет» over coniferous advice
  before saying the latter does not apply. The response took 25 s.

**Diagnosis (log + prompt rebuilt offline on production data):** place detection counted «біля бобрової
греблі … як радять для хвойного лісу» as a comparison (a mitten plus a region) and expanded the coniferous
forest into its three mittens, so the prompt named four target places and presented coniferous insulation
advice as applicable to three of them (9 evidence documents, ≈7,000 prompt tokens); the prompt-only line
from section 13 could not override that. No river-vs-coniferous priority hint was generated in this case,
but the hint pairing compared each document's highest level instead of its level for the same place, so
unrelated places could be ranked. Timing: the answer call took 2.2 s; 22.5 s was waiting on rate limits
(only qwen/qwen3.8-27b was not blocked), and a first attempt 10 s earlier found all models limited.

**Fixes:** a region named as the source of a proposed recommendation for a mitten outside it (an advising
verb or «порада/правило/пам'ятка» just before it, no explicit comparison words, not coordinated with
another place by «і/та/або») is recorded as a **reference**, not a target; the question is scoped to the
mitten, a router "comparison" flag is ignored in that case, and the prompt states that the cited advice
does not apply to the mitten and must not be ranked by priority. Specificity hints are built only between
documents applying to the same place. Offline (keyword-only retrieval) this question's evidence went from
5 to 2 documents, prompt ≈29 % smaller; the effect with production's hybrid retrieval was not measured.

**Tests** (`tests/test_place_reference.py`, 10): detection of three suggestion forms and of unchanged cases
(«Порівняй утеплення у хвойному лісі й біля річки», «Порівняй … біля греблі і в хвойному лісі», coordinated
places without comparison words, a mitten with its own region); hint pairing never ranks different places
or equal-level regions; end to end, the suggestion is answered for the dam only with the reference line and
without coniferous evidence, and a genuine two-region comparison keeps both regions and both insulation
documents without a ranking hint. **These fixes are tested offline only; live verification is pending.**

**Limitation:** reference detection relies on a fixed set of advising words; other phrasings («я чув, що в
хвойному лісі утеплюють мохом») are still treated as a comparison of places.

**Manual retest (developer, 18 Oct 2026, after deploying `4bbc9d0`):**
- riverside suggestion: **passed** (reed, moss rots, coniferous advice does not apply). The question text
  contained a pasted «Надішли /reset, потім:», so no reset actually happened;
- genuine two-region comparison («Порівняй, чим утеплюють рукавички у хвойному лісі та біля річки»):
  **failed**. Both regions' materials were listed, but «У документі сказано: «Утеплюйте рукавичку в жовтні»
  [1].» was inserted in the riverside section and again in the summary, and the riverside item was lost.

**Diagnosis — post-processing, confirmed by the log.** qwen/qwen3.8-27b answered (gpt-oss-120b rate-limited)
and added months although only materials were asked (generation). The month check then flagged three
November sentences, two of them about the river and one naming no place; the correction was rate-limited,
and the fallback replaced each with the coniferous source sentence (the log quotes «Утеплюйте рукавичку в
жовтні.» [3] for each; renumbered to [1] for display). The conflict pair (coniferous insulation vs the
forest-wide calendar) is valid for the coniferous forest only, but the check applied it to any sentence
with November and insulation, regardless of the place the sentence was about. The replacement also did
not notice a quote it had already inserted.

**Fixes:** with evidence for several places (a comparison, several mittens), a sentence is checked against a
conflict pair only if it names that pair's place and no other; otherwise it is left unchanged, never
replaced by another place's rule. A source sentence is inserted at most once. Note on the river: the
forest-wide November is a general rule where no more specific advice exists; it is not the rule for every
riverside mitten — «Під вербою» also belongs to the deciduous forest (after leaf fall, before the first
snow) — so the regression for different valid months is mitten-level («У соснах» vs «Біля бобрової
греблі»).

**Tests** (`tests/test_place_reference.py`, 4 new, final rendered answer): the reconstructed retest output
keeps the riverside materials and preparation with no October and no inserted quote; the expected focused
materials comparison is delivered unchanged with both insulation documents cited and no month; «У соснах»
October and «Біля бобрової греблі» November are both kept; a single-place «У соснах» answer still gets the
October repair, with the quote once. Without the scope check two of them fail, without the deduplication
one. **Live verification pending.** Not addressed in code: months added to a materials comparison (prompt
rules only); in a multi-place answer, a sentence about one place that does not name it is not checked.

**Manual retest (developer, after deploying `be490d9`):** the cross-region October substitution is fixed;
both regions' materials and preparation were correct. Remaining issues: in the summary «хвойний ліс —
жовтень» cited the calendar (which says November); a redundant «У документі сказано: «Кладіть мох і суху
хвою шаром у три лапи»» was inserted; dates were added although only materials were asked.

**Diagnosis (log + offline replay).** gpt-oss-120b answered; the measure check flagged its «Метод укладання»
item and a qwen/qwen3.8-27b correction followed (120b rate-limited); the final text is that correction plus
post-processing. (1) **Citation repair** moved the October clause from the coniferous insulation document
to the calendar (log: «Citation [3] … corrected to [5]»): «жовтень» and «жовтні» had different stems, so the
coniferous document shared no word with the clause, while the calendar contains «жовтень» (for other
activities) and the clause also named «листопадом із загального календаря». (2) The quote was inserted by
the **measure check**: «у хвойному лісі … шар «у три лапи», у річковій частині метод не уточнюється» was read
as denying the coniferous measure, although «не уточнюється» was about the river. (3) The **dates were
generation**; the log does not show whether 120b's first answer or qwen's correction introduced them.

**Fixes.** Month forms share one token. In citation repair a month counts for a document only if one of its
sentences states the month together with a word of the claim's subject (the clause's own words or the
question's); a month match, a place or a document's specificity alone is not evidence, and without such
evidence no citation is assigned or replaced. The measure check works per claim (commas inside numbers do
not split; a place carries over only into subordinate clauses and «там/тут»; a coordinated claim without
a recognised place has an unknown place) and treats "not specified" as a denial only within a claim about
the same subject and place; «річкова частина» is recognised as the river. Comparisons and follow-ups get a
line naming every explicitly requested aspect («коли, чим і яким шаром» → time, materials, thickness;
«чим» → materials and needed preparation; «як часто» → frequency) and asking not to add others except
necessary conditions and exceptions; plain questions keep the completeness rules. Region scopes get a line
that regional advice applies to all member mittens, while local advice and, for «Під вербою», the other
region can add conditions or conflicts.

**What the offline tests show** (`tests/test_scope_attribution.py`, 17): for replayed outputs,
post-processing keeps correct citations and neither inserts nor deletes facts — the October clause keeps the
coniferous citation, no quote is inserted, the expected materials answer has exactly the two insulation
sources; an October insulation claim is not backed by a calendar mentioning October for other activities,
nor by a **more specific** document mentioning October for meetings, and without subject evidence no
citation is reassigned; place contrasts, subordinate clauses and unplaced denials in multi-place answers are
not measure alarms, real denials still are; decimal commas; aspect detection; the prompt lines. Not
flagging «у річковій частині метод не уточнюється» only shows there is no false alarm — it does **not**
verify that statement, which is inaccurate (the river document gives preparation and insulation steps).
**What they cannot show:** that a live model will omit unsolicited dates or follow the aspect line; prompt
assertions only show what the prompt contains.

**Manual retest (developer, 19 Oct 2026, after deploying `c96bb24`): passed.** «Порівняй, чим утеплюють
рукавички у хвойному лісі та біля річки»: materials, thickness, preparation, geographic scope and sources
were correct. The answer's willow-specific addition (dry leaves) was checked by the developer against
«Чергування в рукавичці Під вербою» and is supported.
