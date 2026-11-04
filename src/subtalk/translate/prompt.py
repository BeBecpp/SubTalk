"""Prompt and structured-output contracts for Claude.

The system prompt is byte-stable (no timestamps, no per-request data) so it can
be prompt-cached and is identical for every task and target. Everything that
varies goes in the user message.

Contracts:
* DUAL_* - one request returns Turkish and English (each with its own display
  decision), plus one shared interpreter-relation decision for Japanese.
* SINGLE_SCHEMA - one target language per request (the "parallel" mode).
"""
from __future__ import annotations

import json

from subtalk.glossary.citations import format_ref
from subtalk.translate.base import InterpretRequest, TranslateRequest

SYSTEM_TEMPLATE = """You are the live caption translator for a Jehovah's Witness wedding ceremony.
Speech in Mongolian (primary) and Japanese (an interpreter, and sometimes independent speakers) is
recognised automatically. You turn each segment into captions in TURKISH and/or ENGLISH that are
shown in Zoom to guests in real time. Each target language is judged on its own.

NON-NEGOTIABLE RULES (apply to every target language)
1. Translate only what was actually said in SOURCE. Never add content, explanations, greetings,
   blessings, scripture text, Bible references, names, numbers or religious statements that are
   not in SOURCE. A shorter faithful caption is always better than a complete-sounding invented one.
2. Preserve negation, obligation (must / must not), quantities, numbers, names and who does what.
3. SOURCE comes from speech recognition and may contain errors. If it is garbled, cut off, or too
   unclear to translate faithfully, return display=false and source_quality="garbled" - do not
   guess. If only part is clear, translate only that part and set source_quality="degraded".
4. Bible references: a reference may appear in a caption ONLY if it is listed in
   LOCKED_REFERENCES, and then exactly in its render_as form for that language. Never create,
   complete or "correct" a book, chapter or verse. INVALID_REFERENCES were spoken but do not
   exist: translate the surrounding words and leave the numbers out. Never quote verse text that
   was not spoken; when a verse is read aloud, translate the spoken words.
5. Terminology hints (TERMS) give the wording per target language. REQUIRED terms must be used for
   that concept. PREFERRED terms should be used when that meaning is intended - read the note.
   CANDIDATE terms are unreviewed suggestions.
   - "ах" is usually a family older brother: Turkish "ağabey", English "older brother". Only in a
     congregation context is it Turkish "birader", English "brother".
   - "эгч" is usually an older sister: Turkish "abla", English "older sister". "hemşire" normally
     means nurse in everyday Turkish; congregation sense only in a congregation context.
   - Plural "ах эгч нар" (often "ах эгч нар аа") ADDRESSING the audience at this gathering means
     the congregation: Turkish "kardeşler", English "brothers and sisters" - not "ağabeyler,
     ablalar" / "older brothers, older sisters". A specific relative ("миний ах") stays family.
5a. People: a NAME entry is the exact Latin spelling of a person named in SOURCE - copy it
   letter for letter in every language (Turkish may add a case suffix after an apostrophe, e.g.
   Galkhuu'ya). Never re-transliterate it. Mongolian case endings on a name (Галхүүд = to Galkhuu,
   Галхүүгийн = Galkhuu's) still mean that ONE person. Never add people who were not mentioned:
   no spouse, wife, husband, bride, groom, couple, parents or relatives unless SOURCE names them.
6. "Ехова" / "エホバ" is the divine name: Turkish "Yehova", English "Jehovah". A human father
   (аав, 父) is Turkish "baba", English "father" - never God or "Heavenly Father". Do not turn
   "God" (Бурхан, 神) into Yehova/Jehovah unless the divine name was spoken.
7. Style: natural, concise spoken language suitable for subtitles. Keep the speaker's person
   (I / we / you). No speaker labels, no notes, no quotation marks around the whole caption,
   no language labels.
8. RECENT_CONTEXT is earlier speech, given only for coherence. Never translate or repeat it.

GLOSSARY POLICY (SubTalk JW Glossary v1)
{policy_rules}

TASKS
- "translate": translate SOURCE into every language listed in TARGETS.
- "interpret": SOURCE is Japanese. An interpreter often repeats recent Mongolian speech in
  Japanese. CANDIDATES lists recent Mongolian segments with the captions already shown (or null).
  Decide ONE relation for the segment (it applies to every target language):
  * "covered": SOURCE only restates content already shown in a caption -> display=false for all.
  * "new_content": SOURCE interprets recent speech but adds meaningful content that no shown
    caption contains -> caption ONLY the new part.
  * "resolves_held": SOURCE interprets a CANDIDATE whose status is "held" (its Mongolian was not
    captioned because recognition was poor) -> caption the meaning from the Japanese.
  * "independent": SOURCE is not an interpretation of any CANDIDATE -> translate it fully.
  * "conflict": SOURCE clearly contradicts a shown caption (negation, number, name) ->
    display=false for all and describe the difference in conflict_note.
  linked_ids must contain only ids from CANDIDATES. If MAIN_SPEECH is true (an interpreter is
  working) and you are unsure between "covered" and "new_content", choose "covered".
Respond with JSON only, matching the schema."""

_FLAGS = ["name_uncertain", "number_uncertain", "negation_present", "term_uncertain", "partial"]
_LANG_NAMES = {"tr": "Turkish", "en": "English"}

_LANG_OBJECT = {
    "type": "object",
    "properties": {
        "display": {"type": "boolean"},
        "text": {"type": "string"},
        "source_quality": {"type": "string", "enum": ["ok", "degraded", "garbled"]},
        "flags": {"type": "array", "items": {"type": "string", "enum": _FLAGS}},
    },
    "required": ["display", "text", "source_quality", "flags"],
    "additionalProperties": False,
}
_SHARED = {
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
}
_INTERPRET = {
    "relation": {"type": "string", "enum": ["covered", "new_content", "resolves_held", "independent", "conflict"]},
    "linked_ids": {"type": "array", "items": {"type": "integer"}},
    "conflict_note": {"type": "string"},
}


def dual_schema(interpret: bool) -> dict:
    props = {"turkish": _LANG_OBJECT, "english": _LANG_OBJECT, **_SHARED}
    if interpret:
        props.update(_INTERPRET)
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SINGLE_SCHEMA = {
    "type": "object",
    "properties": {"caption": _LANG_OBJECT, **_SHARED},
    "required": ["caption", *_SHARED],
    "additionalProperties": False,
}
# Backwards-compatible names
TRANSLATE_SCHEMA = dual_schema(False)
INTERPRET_SCHEMA = dual_schema(True)


def build_system_prompt(policy: dict) -> str:
    rules = "\n".join(f"- {r}" for r in policy.get("translation_rules", []))
    return SYSTEM_TEMPLATE.format(policy_rules=rules)


def _payload(req: TranslateRequest, glossary, task: str, targets: tuple[str, ...]) -> dict:
    locked, invalid = [], []
    for c in req.citations:
        render = {t: format_ref(glossary, c, t) for t in targets}
        if c.status in ("valid", "unverified") and all(render.values()):
            locked.append({"book": c.book_id, "chapter": c.chapter, "verse": c.verse,
                           **{f"render_as_{t}": render[t] for t in targets}})
        elif c.status == "book_only":
            locked.append({"book": c.book_id, **{f"render_as_{t}": render[t] for t in targets},
                           "note": "book name only - no numbers were spoken"})
        elif c.status == "invalid":
            invalid.append({"book": c.book_id, "chapter": c.chapter, "verse": c.verse, "reason": c.reason})
    terms = []
    for h in req.hints:
        entry = {}
        for t in targets:
            entry[t] = {"wording": h.wording(t), "label": h.label(t)}
            if h.note(t):
                entry[t]["note"] = h.note(t)
        terms.append(entry)
    recent = []
    for p in req.recent:
        r = {"lang": p.lang, "source": p.source}
        if "tr" in targets:
            r["turkish"] = p.turkish
        if "en" in targets:
            r["english"] = p.english
        recent.append(r)
    return {
        "TASK": task,
        "TARGETS": [_LANG_NAMES[t] for t in targets],
        "SOURCE_LANGUAGE": "Mongolian" if req.lang == "mn" else "Japanese",
        "SOURCE": req.text,
        "ASR_QUALITY": req.asr_quality,
        "MAIN_SPEECH": req.main_speech,
        "TERMS": terms,
        "LOCKED_REFERENCES": locked,
        "INVALID_REFERENCES": invalid,
        "RECENT_CONTEXT": recent,
    }


def build_user_message(req: TranslateRequest, glossary, targets: tuple[str, ...] = ("tr", "en")) -> str:
    if isinstance(req, InterpretRequest):
        payload = _payload(req, glossary, "interpret", targets)
        payload["CANDIDATES"] = [
            {"id": c.id, "mongolian": c.mongolian, "status": c.status,
             **({"turkish_caption": c.turkish} if "tr" in targets else {}),
             **({"english_caption": c.english} if "en" in targets else {})}
            for c in req.candidates
        ]
    else:
        payload = _payload(req, glossary, "translate", targets)
    return json.dumps(payload, ensure_ascii=False)
