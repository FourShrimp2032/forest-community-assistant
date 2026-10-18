# Agreed requirements — Forest Community Document Assistant

Summary of the behaviour agreed with the customer (original brief plus the clarifications received
during implementation). The README describes how each point is implemented.

## Purpose
- Telegram bot (private chats) for fictional animal communities — «рукавички», each a separate ОСМД.
- Answers questions from shared documents, always with sources; accepts new documents; manages versions.
- All user-facing messages are in Ukrainian; original place names and document titles are kept.

## Technical constraints
- Python; LLM from Groq's free tier, model configurable (verified availability before choosing the default).
- Whole application starts with `docker compose up --build`; secrets via environment variables;
  `.env.example` without credentials.
- Documents, metadata, user names and state persist across restarts.
- No required paid service; reliable and simple over extra infrastructure.

## Geography (maintained separately from documents)

| Mitten | Regions |
|---|---|
| Під ялиною | Хвойний ліс |
| У соснах | Хвойний ліс |
| Біля мурашника | Хвойний ліс |
| Під дубом | Листяний ліс |
| У ліщині | Листяний ліс |
| Під вербою | Листяний ліс; Біля річки |
| Біля бобрової греблі | Біля річки |
| На узліссі | — |

- «Під вербою» belongs to both regions; «На узліссі» to none (only its own and forest-wide advice apply).
- Region advice never leaks to unrelated mittens; passage-level scope is preserved inside documents.
- Questions may concern one mitten, a region, the whole forest or several mittens; users are not forced
  to pick one mitten. Follow-ups reuse the conversation's place; a newly stated place overrides it; when a
  needed place is ambiguous the bot asks. Short names ("біля греблі") are understood.

## Priority and conflicts
1. Mitten advice > 2. region advice > 3. forest-wide advice. Source channel does not matter.
- Complementary advice is combined; more specific advice wins over conflicting broader advice.
- Equally specific conflicting advice: show both with sources and explain.
- «Під вербою»'s two regions: the brief states that no priority between them was agreed and names the
  equal-specificity conflict rule as the implementation default. Showing both with sources is therefore
  our implementation default, not a separate customer confirmation.
- A newer unrelated document does not suppress another one just because topics overlap.

## Documents
- Formats: PDF, DOCX, TXT (no legacy `.doc`). **OCR approved** for PDFs without a usable text layer
  (Drive and Telegram). Photo uploads are out of scope.
- Unreadable files are never used: Telegram → the sender is notified; Drive → shown in `/pending`
  with the reason; an unreadable replacement candidate leaves the previous valid version active.
- Google Drive «Лісовий архів» (`1b_hEjnkQfP8ZVP-OEbnYWqoGSFwXMAP3`) is ingested automatically: at startup
  and periodically (default 10 min, configurable); new files, updates and confirmed deletions are detected.
- **Drive access:** Drive API with an API key from the developer's own Google Cloud project
  (`GOOGLE_DRIVE_API_KEY`, `GOOGLE_DRIVE_FOLDER_ID`); no OAuth/service account unless a verified limitation
  requires it; no page scraping. Reviewers' willingness to create their own key is not confirmed.
- Any Telegram user may upload; uploads are shared with everyone but apply only to their scope.

## Uploader identity
- Before a user's first upload: ask separately for the real name (never substitute the Telegram profile
  name), keep the pending upload, remember the name for later uploads; ownership by Telegram user ID.
- No name is needed for questions. Telegram sources show the self-reported name and receipt time.

## Location of documents
- Determined automatically when explicit/unambiguous. Otherwise:
  - Telegram: ask the sender (mitten(s), region(s) or whole forest); unused until resolved.
  - Drive: do not guess; mark pending; keep the previous valid version active; show in `/pending`;
    reprocess when a later sync detects a change to the file.

## Versioning
- Version timestamps: Drive file modification time; Telegram message receipt time (never download time).
  Normalised to UTC; displayed in Europe/Kyiv.
- Replacement signals: same title/name **and** same scope; or an explicit update statement in the content.
  Same topic + same place alone is not enough. Stable Drive file IDs track updates of the same file.
- **Cross-source and cross-user replacement is allowed**: a genuinely newer version of the same logical
  document for the same place replaces the previous one regardless of source or uploader.
- Ambiguous identity: Telegram → ask the uploader "replaces / separate" (whoever owns the existing
  document); Drive → never supersede automatically, keep both, show conflicts with sources.
- Replacement confirmation does not bypass the timestamp check; an older version never replaces a newer
  one; re-ingesting identical content creates no version and does not make a document newer.
- Superseded versions are never used; they are not reactivated automatically when the active version is
  withdrawn or deleted (explicitly accepted by the customer).

## Drive deletions and failures
- Only a successful, complete listing may establish that a file is gone; then its documents stop being
  used. Failed/partial listings are not evidence of deletion; last valid documents stay available; failures
  are logged.

## Withdrawal
- `/my_documents` lists the user's uploads; withdrawal with confirmation, authorised by Telegram user ID;
  only own Telegram uploads. Withdrawn documents stop contributing for everyone; no automatic restore.
- A "get file" button of an old answer for a withdrawn document replies «Документ відкликано»; earlier
  Telegram messages are not changed.

## Answers
- Concise; all material details (amounts, deadlines, conditions, alternatives, exceptions, place);
  multiple documents combined with attribution per piece of advice.
- Sources from stored records: Drive — title, «Лісовий архів», link; Telegram — title, uploader's real
  name, receipt time, a way to get the file; place where relevant.
- Not in documents → say it was not found; no outside knowledge; ask when missing details block search.
- Off-topic → brief explanation and a relevant example question.
- Comparisons: evidence per place, conditions, sources, missing data stated, no forest-wide minimum from
  partial data, no unit conversions (acorns vs nuts…).

## Commands
`/start`, `/help`, `/reset` (clears context and place; keeps documents and name), `/pending`,
`/my_documents`. Location selection via buttons, text accepted too; large lists paginated. Ordinary
questions and uploads need no commands. No admin interface.

## Customer examples (checked against the actual archive)
- «Скільки внесок під дубом і до якого числа?» → 4 жолуді з дорослого звіра на місяць, до 10 числа,
  Кабанові в кошик біля входу; замість 4 жолудів — 2 лісові горіхи.
- «Біля греблі мисливці! Як кричати?» → тричі коротко, як Сорока; почув постріл — у гущавину.
- «Де шукати гриби біля верби?» → local: опеньки на корінні верби (priority) + deciduous and riverside guides.
- «Коли утеплювати рукавичку на узліссі?» → no local advice; forest-wide «Календар сезонів»: у листопаді.

## Defaults accepted
Europe/Kyiv; Telegram long polling; SQLite + files on a Docker volume; retrieval by geography and active
status + keyword search + local multilingual embeddings; Drive sync at startup and every 10 minutes.
