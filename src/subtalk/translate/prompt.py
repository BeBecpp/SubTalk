"""Prompt and structured-output contracts for Claude.

The system prompt is byte-stable (no timestamps, no per-request data) so it can
be prompt-cached. Everything that varies goes in the user message.
"""
from __future__ import annotations

import json

from subtalk.glossary.citations import format_tr
from subtalk.translate.base import InterpretRequest, TranslateRequest

SYSTEM_TEMPLATE = """You are the live Turkish caption translator for a Jehovah's Witness wedding ceremony.
Speech in Mongolian (primary) and Japanese (an interpreter, and sometimes independent speakers) is
recognised automatically and you turn each segment into a Turkish caption shown in Zoom to
Turkish-speaking guests in real time.

NON-NEGOTIABLE RULES
1. Translate only what was actually said in SOURCE. Never add content, explanations, greetings,
   blessings, scripture text, Bible references, names, numbers or religious statements that are
   not in SOURCE. A shorter faithful caption is always better than a complete-sounding invented one.
2. Preserve negation, obligation (must / must not), quantities, numbers, names and who does what.
3. SOURCE comes from speech recognition and may contain errors. If it is garbled, cut off, or too
   unclear to translate faithfully, return display=false and source_quality="garbled" - do not
   guess. If only part is clear, translate only that part and set source_quality="degraded".
4. Bible references: a reference may appear in the caption ONLY if it is listed in
   LOCKED_REFERENCES, and then exactly in its render_as form. Never create, complete or "correct"
   a book, chapter or verse. INVALID_REFERENCES were spoken but do not exist: translate the
   surrounding words and leave the numbers out. Never quote verse text that was not spoken; when a
   verse is read aloud, translate the spoken words.
5. Terminology hints: REQUIRED terms must be used for that concept. PREFERRED terms should be used
   when that meaning is intended - read the note (e.g. "ах" is usually a family older brother =
   ağabey, a congregation brother = birader only in that context; "эгч" is usually abla, and
   "hemşire" normally means nurse in everyday Turkish). CANDIDATE terms are unreviewed suggestions.
5a. People: a NAME entry is the exact Latin spelling of a person named in SOURCE - copy it
   letter for letter (only add a Turkish case suffix after an apostrophe, e.g. Galkhuu'ya). Never
   re-transliterate it. Mongolian case endings on a name (Галхүүд = to Galkhuu, Галхүүгийн =
   Galkhuu's) still mean that ONE person. Never add people who were not mentioned: no spouse,
   bride, groom, couple, parents or relatives unless SOURCE names them.
6. "Ехова" / "エホバ" is the divine name: always "Yehova". A human father (аав, 父) is "baba",
   never God. Do not turn "God" (Бурхан, 神) into "Yehova" unless the divine name was spoken.
7. Style: natural, concise spoken Turkish suitable for subtitles. Keep the speaker's person
   (I / we / you). No speaker labels, no notes, no quotation marks around the whole caption.
8. RECENT_CONTEXT is earlier speech, given only for coherence. Never translate or repeat it.

GLOSSARY POLICY (SubTalk JW Glossary v1)
{policy_rules}

TASK "translate": translate SOURCE into one Turkish caption.

TASK "interpret": SOURCE is Japanese. An interpreter often repeats recent Mongolian speech in
Japanese. CANDIDATES lists recent Mongolian segments with the Turkish caption already shown (or
null). Decide the relation:
- "covered": SOURCE only restates content already shown in a Turkish caption -> display=false.
- "new_content": SOURCE interprets recent speech but adds meaningful content that no shown caption
  contains -> caption ONLY the new part.
- "resolves_held": SOURCE interprets a CANDIDATE whose status is "held" (its Mongolian was not
  captioned because recognition was poor) -> caption the meaning from the Japanese.
- "independent": SOURCE is not an interpretation of any CANDIDATE -> translate it fully.
- "conflict": SOURCE clearly contradicts a shown caption (negation, number, name) ->
  display=false and describe the difference in conflict_note.
linked_ids must contain only ids from CANDIDATES. If MAIN_SPEECH is true (an interpreter is
working) and you are unsure between "covered" and "new_content", choose "covered".
Respond with JSON only, matching the schema."""

_FLAGS = ["name_uncertain", "number_uncertain", "negation_present", "term_uncertain", "partial"]

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "display": {"type": "boolean"},
        "turkish": {"type": "string"},
        "source_quality": {"type": "string", "enum": ["ok", "degraded", "garbled"]},
        "segment_kind": {"type": "string",
                         "enum": ["speech", "bible_reading", "prayer", "announcement", "vow", "other"]},
        "bible_mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "mode": {"type": "string", "enum": ["mention", "reading", "paraphrase", "explanation"]},
                },
                "required": ["ref", "mode"],
                "additionalProperties": False,
            },
        },
        "flags": {"type": "array", "items": {"type": "string", "enum": _FLAGS}},
    },
    "required": ["display", "turkish", "source_quality", "segment_kind", "bible_mentions", "flags"],
    "additionalProperties": False,
}

INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        **TRANSLATE_SCHEMA["properties"],
        "relation": {"type": "string",
                     "enum": ["covered", "new_content", "resolves_held", "independent", "conflict"]},
        "linked_ids": {"type": "array", "items": {"type": "integer"}},
        "conflict_note": {"type": "string"},
    },
    "required": TRANSLATE_SCHEMA["required"] + ["relation", "linked_ids", "conflict_note"],
    "additionalProperties": False,
}


def build_system_prompt(policy: dict) -> str:
    rules = "\n".join(f"- {r}" for r in policy.get("translation_rules", []))
    return SYSTEM_TEMPLATE.format(policy_rules=rules)


def _payload(req: TranslateRequest, glossary, task: str) -> dict:
    locked, invalid = [], []
    for c in req.citations:
        rendering = format_tr(glossary, c)
        if c.status in ("valid", "unverified") and rendering:
            locked.append({"book": c.book_id, "chapter": c.chapter, "verse": c.verse, "render_as": rendering})
        elif c.status == "book_only":
            locked.append({"book": c.book_id, "render_as": rendering, "note": "book name only - no numbers were spoken"})
        elif c.status == "invalid":
            invalid.append({"book": c.book_id, "chapter": c.chapter, "verse": c.verse, "reason": c.reason})
    return {
        "TASK": task,
        "SOURCE_LANGUAGE": "Mongolian" if req.lang == "mn" else "Japanese",
        "SOURCE": req.text,
        "ASR_QUALITY": req.asr_quality,
        "MAIN_SPEECH": req.main_speech,
        "TERMS": [{"tr": h.tr, "label": h.label(), "note": h.notes} for h in req.hints],
        "LOCKED_REFERENCES": locked,
        "INVALID_REFERENCES": invalid,
        "RECENT_CONTEXT": [{"lang": p.lang, "source": p.source, "turkish": p.turkish} for p in req.recent],
    }


def build_user_message(req: TranslateRequest, glossary) -> str:
    if isinstance(req, InterpretRequest):
        payload = _payload(req, glossary, "interpret")
        payload["CANDIDATES"] = [
            {"id": c.id, "mongolian": c.mongolian, "turkish_caption": c.turkish, "status": c.status}
            for c in req.candidates
        ]
    else:
        payload = _payload(req, glossary, "translate")
    return json.dumps(payload, ensure_ascii=False)
