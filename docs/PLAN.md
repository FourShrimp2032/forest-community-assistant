# Implementation plan (as executed)

1. **Inspect the archive.** 78 files (39 PDF, 18 DOCX, 21 TXT), one page each. 19 PDFs are scanned images
   with a textured background and no text layer → OCR needed (approved). Titles name a mitten or region for
   68 documents; 10 state their audience in the first sentence (forest, region). The archive contains
   deliberate traps: insulation timing differs per level; «Під вербою» gets two regional mushroom guides
   that disagree; «Броди…» (riverside) has a willow-only section; the dam mitten forbids wading while the
   riverside document allows it when the dam is open.
2. **Extraction.** pypdf (text layer, reflow of wrapped lines), python-docx, TXT (UTF-8/CP1251). OCR with
   Tesseract `ukr` on rendered pages: several preprocessing variants (thresholds + colour mask); main text
   from the most confident variant; every number decided by confidence-weighted vote across variants
   (fixes texture blobs turning «5» into «95»); low confidence → document rejected.
3. **Geography & scope.** Mapping in code; deterministic document scope (title → audience sentence),
   LLM fallback only with a verified verbatim quote; passage-level scopes for place headings.
4. **Lifecycle.** SQLite rows per document version; statuses pending/active/superseded/withdrawn/removed/
   failed/duplicate; replacement rules with source timestamps; decision history per document.
5. **Drive sync.** API-key listing (all pages, sub-folders), skip unchanged, download + ingest changed,
   deletions only after a complete listing, safety guard against mass deletion.
6. **Retrieval.** Only active chunks; applicability per target mitten/region; guaranteed slot per specificity
   level; BM25 + fastembed multilingual embeddings; evidence budget for Groq free-tier limits.
7. **Answering.** Router LLM (small model) → place/intent/keywords merged with deterministic detection;
   answer LLM gets numbered evidence with level labels and strict rules; sources rendered from DB.
8. **Dialogue + Telegram.** Transport-independent `Conversation` (name collection, clarifications,
   withdrawal, pagination) + thin python-telegram-bot adapter.
9. **Tests, Docker, docs.** Deterministic tests on the real archive texts; optional live tests; README.
