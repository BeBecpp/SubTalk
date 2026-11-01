# JW glossary integration

The supplied **SubTalk JW Glossary v1** lives unchanged in `resources/glossary/` (its own
`validate.py` runs in CI). SubTalk loads it once at startup and refuses to start if it is invalid
(66 books, unique IDs, valid statuses/priorities, known source keys, `full_verse_texts_included: false`).

## Review status

| Status | Meaning | How Claude sees it |
|---|---|---|
| `review_required` (38 terms) | Candidate wording | `CANDIDATE (unreviewed - use only if it fits naturally)` |
| `tr_attested` (48 terms) | Turkish wording appears in cited JW.org sources | `REQUIRED` (priority always) / `PREFERRED (when this meaning is intended)` |
| `human_approved` | Signed off by a Turkish JW reviewer | same as attested |

`subtalk glossary-check` lists everything still `review_required`. A reviewer approves terms in
`event/glossary_approvals.json` (gitignored):

```json
{"reviewer": "Name", "date": "2026-11-04", "approved_term_ids": ["amen", "faith", "covenant"]}
```

Names: copy `resources/glossary/event_names.example.json` to `event/event_names.json`, confirm the
Latin spellings with the family and set `"approved": true`. Until then the dashboard warns and every
caption with a name is flagged `name_spelling_unapproved`.

## Term selection

Only terms whose Mongolian/Japanese form occurs in the segment are sent (max 14), never the whole
glossary or the 66-book list. Matching handles Mongolian case suffixes and fleeting vowels
(тангараг → тангаргаа), but short words only take known case suffixes ("ах" never matches
"ахлагч"). Single-kanji Japanese terms do not match inside longer words (神 ≠ 神戸).

## Bible references

Deterministic parser (`glossary/citations.py`):

* Book by exact name or alias only; numbered books (1 Коринт, Нэгдүгээр Коринт, コリント第一).
* Chapter/verse from digits (5:33), Mongolian number words (тавдугаар бүлэг гучин гуравдугаар
  зүйл, хоёрын хорин дөрөв) or Japanese (5章33節, 十三章四節).
* Book names that are also ordinary words (Эхлэл "beginning", Дуулал "song") count only with
  numbers, a chapter word or a Bible cue.
* Validation: chapter counts for all 66 books (`resources/bible/chapter_counts.json`); verse counts
  for a small curated set (`verse_counts.json`). Unknown verse count → `unverified` (allowed only
  because the numbers were spoken). Out of range → `invalid`: never rendered, never "corrected".
* Turkish rendering uses the canonical 2025 names (Efesliler 5:33); legacy names in output
  (Efesoslular) are rewritten only in reference position.

**Add every reference planned for the ceremony to `verse_counts.json`** after checking it on jw.org.

## Regression tests

`tests/test_glossary.py` turns the glossary's evaluation cases into tests for book resolution,
invalid references, references without numbers, Bible-cue requirements, divine name handling, human
father vs God, vows, prayer, numbers and negation, names, length, and Japanese citations. Cases that
depend on translation quality (ağabey vs birader, nurse vs sister) cannot be checked
deterministically - they belong in the M0-B Turkish review with live Claude.
