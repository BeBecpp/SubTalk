# Claude Code integration task: SubTalk glossary

Use the JSON resources in this directory as a **read-only, deterministic terminology layer**. Do NOT rewrite or translate the glossary from scratch using the LLM.

Build in this order:

1. Load `bible_books.json`, `jw_terms.json`, `wedding_terms.json`, `translation_policy.json`, `event_names.example.json` with standard `json` at startup and validate each entry. Keep the loaded resources in memory.
2. Write a `GlossaryService` with normalized matching (Unicode NFC, Mongolian `ё`/`е` variations where explicitly listed, normalized whitespace, Japanese kana variants where explicitly listed, Turkish dotted/dotless i under a locale-aware normalizer). Don't aggressively fuzzy-match short religious terms; avoid false positives.
3. Deterministic exact/alias book lookup returns `book.id`, `book.tr`. It must NOT guess book IDs from generic topic descriptions. Require an explicit Bible-reference cue and resolved numbers before captioning scripture refs.
4. Select only source-triggered glossary terms for each transcription chunk. Include a minimal always-on list for identity and name normalization **only when referents are present**. Do not send the 66-book index or all terms to Claude on each chunk. Keep a bounded context window and cache stable glossary instructions as supported.
5. Tell Claude API to preserve meaning, negation, quantities, and names, without adding unspoken scripture. Treat model output as untrusted until guards check it.
6. `review_required` terms are candidate translations, not forced canonical JW terminology. Allow configuration to promote individual entries to `human_approved` after a Turkish JW reviewer signs off.
7. When a scripture reference is detected, parse book/chapter/verse deterministically. Validate known referenced verses using a separately curated metadata table. This pack **does not include a complete chapter/verse-count database**. Missing data => do not assert validation. No auto-quoting of full copyrighted verses.
8. `event_names.example.json` has `approved:false`. Prompt an authorized operator during setup to verify the Latin spellings; never fabricate Akane's kanji.
9. At runtime `Main Speech` only changes script-context retrieval. General untranslated/unscripted Mongolian speech must continue to translate.
10. Cover all critical ambiguity regressions: Turkish Bible books (Efesliler/Efesoslular), human father vs God, JW brother vs human older brother, vow vs promise, reference mention vs reading, nonexistent verse, uncertainty, numerical negations, mixed JA/MN language, one source event -> one subtitle.

Work out an interface and tests first, but do not change the established Zoom/Whisper engine state machine. Do not add a database or OBS.
