"""Live Claude evaluation: Turkish and English (optionally Japanese), checked independently.

Runs each case through the production path (glossary analysis -> Claude ->
deterministic guards, per target language) and applies an automatic check per
case and language. Automatic checks are heuristics that catch clear failures;
a Turkish and an English speaker still have to read the output.

Cases: the 31 synthetic cases of SubTalk JW Glossary v1 plus SubTalk's own
adversarial cases (EXTRA_CASES) aimed at subtle semantic failures.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from subtalk.glossary.guards import Guards
from subtalk.glossary.normalize import turkish_lower
from subtalk.glossary.service import GlossaryService
from subtalk.translate.base import (
    ALL_TARGETS,
    TARGETS,
    BudgetExceeded,
    InterpretRequest,
    MnCandidate,
    TranslateRequest,
    TranslatorError,
)

NEG = {
    # Turkish negation (heuristic): negative verb suffixes, değil, yok, gerekmez/olmaz.
    "tr": re.compile(r"\w+(m[ae]d[ıiuü]|m[ae]m[ıiuü]ş|m[ae]z\b|m[ae]y[ıiuü]|m[ae]l[ıi]|m[ae]yece|m[ae]yaca)\w*"
                     r"|\bdeğil|\byok\b|\bgerekmez|\bolmaz|\basla\b|\bhiç\b"),
    "en": re.compile(r"\b(not|never|no|nobody|nothing|neither|nor|cannot)\b|n['’]t\b"),
    # Japanese negation (heuristic): ない / ません / ず / なかった / いけない / なりません.
    "ja": re.compile(r"ない|ません|ず|ぬ|なかった|いけ|なりません"),
}
DIGITS = re.compile(r"\d")

# case id -> per-language check spec. Strings are lower-cased substrings of " text ".
CHECKS: dict[str, dict[str, dict]] = {
    "divine_name": {"tr": {"must": ["yehova"]}, "en": {"must": ["jehovah"]}},
    "human_father": {"tr": {"must": ["baba"], "must_not": ["tanrı", "yehova", "rab "]},
                     "en": {"must": ["father"], "must_not": ["god", "jehovah", "lord", "heavenly"]}},
    "god_not_jehovah": {"tr": {"must": ["tanrı"], "must_not": ["yehova"]},
                        "en": {"must": ["god"], "must_not": ["jehovah"]}},
    "brother_family": {"tr": {"must": ["ağabey"], "must_not": ["birader"]},
                       "en": {"must_any": [["older brother", "elder brother", "big brother"]],
                              "must_not": ["congregation"]}},
    "brother_congregation": {"tr": {"must_not": ["galkhuu", "damat"]},
                             "en": {"must": ["brother"], "must_not": ["galkhuu", "groom"]}},
    "sister_not_nurse": {"tr": {"must_not": ["hemşire"]}, "en": {"must": ["sister"], "must_not": ["nurse"]}},
    "elder_office": {"tr": {"must": ["ihtiyar"]}, "en": {"must": ["elder"]}},
    "ephesians_alias": {"tr": {"must": ["efesliler 5:33"], "must_not": ["efesoslular"]},
                        "en": {"must": ["ephesians 5:33"]}},
    "ephesians_current": {"tr": {"must": ["efesliler 5:33"]}, "en": {"must": ["ephesians 5:33"]}},
    "first_corinthians": {"tr": {"must": ["1. korintliler 13:4"], "must_not": ["korintoslular"]},
                          "en": {"must": ["1 corinthians 13:4"]}},
    "genesis": {"tr": {"must": ["başlangıç 2:24"]}, "en": {"must": ["genesis 2:24"]}},
    "deuteronomy": {"tr": {"must": ["kanun'un tekrarı 6:4"]}, "en": {"must": ["deuteronomy 6:4"]}},
    "revelation": {"tr": {"must": ["vahiy 21:4"]}, "en": {"must": ["revelation 21:4"], "must_not": ["revelations"]}},
    "negation": {"tr": {"negation": True}, "en": {"negation": True}},
    "obligation_negation": {"tr": {"negation": True}, "en": {"negation": True}},
    "vow_not_inferred": {"tr": {"must_not": ["yemin"]}, "en": {"must_not": ["vow"]}},
    "explicit_vow": {"tr": {"must": ["yemin"]}, "en": {"must": ["vow"]}},
    "bible_explanation": {"tr": {"negation": True, "no_digits": True}, "en": {"negation": True, "no_digits": True}},
    # The non-existent verse must not appear in digits OR words (4 Nov: "verse thirty-four" slipped through).
    "invalid_reference": {"tr": {"must_not": ["5:33", "5:34", "34"], "must_not_re": [r"otuz\s*(dört|üç)"]},
                          "en": {"must_not": ["5:33", "5:34", "34"], "must_not_re": [r"thirty[\s-]*(four|three)"]}},
    "book_no_verse": {"tr": {"no_digits": True}, "en": {"no_digits": True}},
    "prayer": {"tr": {"must": ["dua"]}, "en": {"must": ["pray"]}},
    "title_reference": {"tr": {"must": ["kutsal kitap"], "no_digits": True},
                        "en": {"must": ["bible principles"], "no_digits": True}},
    "mc_welcome": {"tr": {"must_not": ["yehova", "tanrı", "dua"]}, "en": {"must_not": ["jehovah", "god", "pray"]}},
    "groom_name": {"tr": {"must": ["galkhuu"], "must_not_re": [r"\beş\w*", r"\bkarı\w*", r"\bgelin\b", r"\bçift\w*"]},
                   "en": {"must": ["galkhuu"], "must_not_re": [r"\bwife\b", r"\bspouse\b", r"\bbride\b", r"\bcouple\b"]}},
    "akane_name": {"tr": {"must": ["akane"], "must_not_re": [r"\beş\w*", r"\bkoca\w*", r"\bdama[dt]\w*", r"\bçift\w*"]},
                   "en": {"must": ["akane"], "must_not_re": [r"\bhusband\b", r"\bspouse\b", r"\bgroom\b", r"\bcouple\b"]}},
    "japanese_independent": {"tr": {"display": True}, "en": {"display": True}},
    "japanese_repetition": {"*": {"relation": "covered"}},
    "plain_contract": {"tr": {"must_not": ["ahit", "ahd"]}, "en": {"must_not": ["covenant"]}},
    "numerical_difference": {"tr": {"must_any": [["30", "otuz"], ["10", " on "]], "negation": True},
                             "en": {"must_any": [["30", "thirty"], ["10", "ten"]], "negation": True}},
}
# Speech-free cases belong to the ASR layer (tests/test_langid.py), not translation.
NOT_TEXT = {"music_only", "applause_only"}

# SubTalk adversarial cases: subtle failures (synthetic sentences).
EXTRA_CASES: list[dict] = [
    {"id": "adv_father_is_not_god", "language": "mn", "source_text": "Аав минь намайг үргэлж хайрладаг байсан.",
     "checks": {"tr": {"must": ["baba"], "must_not": ["tanrı", "yehova"]},
                "en": {"must": ["father"], "must_not": ["god", "jehovah", "heavenly"]}}},
    {"id": "adv_prohibition", "language": "mn", "source_text": "Хэрүүл маргааныг хэзээ ч бүү үргэлжлүүл.",
     "checks": {"tr": {"negation": True}, "en": {"negation": True}}},
    {"id": "adv_numbers_years", "language": "mn",
     "source_text": "Бид арван хоёр жилийн өмнө анх танилцсан.",
     "checks": {"tr": {"must_any": [["12", "on iki"]]}, "en": {"must_any": [["12", "twelve"]]}}},
    {"id": "adv_reference_mention_not_quote", "language": "mn",
     "source_text": "Эфэс 5:33-т гэрлэлтийн тухай зөвлөгөө бий.",
     # A faithful rendering is ~45-70 characters; quoting the verse itself would be far longer.
     "checks": {"tr": {"must": ["efesliler 5:33"], "max_len": 100}, "en": {"must": ["ephesians 5:33"], "max_len": 100}}},
    {"id": "adv_no_vow_from_love", "language": "mn", "source_text": "Тэд бие биенээ үнэхээр хүндэлдэг.",
     "checks": {"tr": {"must_not": ["yemin", "söz ver"]}, "en": {"must_not": ["vow", "promise"]}}},
    {"id": "adv_name_genitive", "language": "mn", "source_text": "Аканегийн гэр бүл Японоос ирсэн.",
     "checks": {"tr": {"must": ["akane"], "must_not_re": [r"\bkoca\w*", r"\bdama[dt]\w*"]},
                "en": {"must": ["akane"], "must_not_re": [r"\bhusband\b", r"\bgroom\b"]}}},
    {"id": "adv_japanese_new_detail", "language": "ja", "interpret_context": True,
     "source_text": "結婚生活では愛と尊敬が大切です。特に毎日感謝を伝えてください。",
     "checks": {"*": {"relation_in": ["new_content", "independent"]},
                "tr": {"display": True}, "en": {"display": True}}},
    {"id": "adv_jehovah_witnesses_org", "language": "mn", "source_text": "Еховагийн Гэрчүүдийн хурлаас олон ах эгч нар ирсэн.",
     "checks": {"tr": {"must": ["yehova"]}, "en": {"must": ["jehovah"]}}},
    # English-focused high-risk cases (3 Nov course correction). Terms follow jw.org English usage.
    {"id": "en_jehovah_loves_us", "language": "mn", "source_text": "Ехова биднийг хайрладаг.",
     "checks": {"tr": {"must": ["yehova", "sev"]}, "en": {"must": ["jehovah", "love"], "must_not": ["lord"]}}},
    {"id": "en_father_gives_talk", "language": "mn", "source_text": "Миний аав өнөөдөр илтгэл тавина.",
     "checks": {"tr": {"must": ["baba", "konuşma"], "must_not": ["tanrı", "yehova", "vaaz"]},
                "en": {"must": ["father", "talk"], "must_not": ["god", "jehovah", "heavenly", "sermon"]}}},
    {"id": "en_holy_spirit", "language": "mn", "source_text": "Ариун сүнс бидэнд хүч өгдөг.",
     "checks": {"tr": {"must": ["kutsal ruh"]}, "en": {"must": ["holy spirit"], "must_not": ["holy ghost"]}}},
    {"id": "en_jesus_christ", "language": "mn", "source_text": "Есүс Христ бидэнд сайхан үлгэр жишээ үзүүлсэн.",
     "checks": {"tr": {"must": ["isa"]}, "en": {"must": ["jesus", "example"]}}},
    {"id": "en_husband_wife", "language": "mn", "source_text": "Нөхөр эхнэр хоёр бие биеэ хүндэлж байх ёстой.",
     "checks": {"tr": {"must": ["koca"]}, "en": {"must": ["husband", "wife", "respect"]}}},
    {"id": "en_elder_talk", "language": "mn", "source_text": "Хурлын ахлагч ах хуримын илтгэлийг тавина.",
     "checks": {"tr": {"must": ["ihtiyar"], "must_not": ["başkan"]},
                "en": {"must": ["elder", "talk"], "must_not": ["president", "chairman", "leader", "sermon"]}}},
    {"id": "en_marriage_not_contract", "language": "mn", "source_text": "Гэрлэлт бол зүгээр нэг гэрээ биш.",
     "checks": {"tr": {"must": ["evlilik"], "negation": True}, "en": {"must": ["marriage"], "negation": True}}},
    {"id": "en_promise_not_vow", "language": "mn", "source_text": "Галхүү Аканед өгсөн амлалтаа биелүүлнэ гэж хэлсэн.",
     "checks": {"tr": {"must": ["galkhuu", "akane", "söz"]},
                "en": {"must": ["galkhuu", "akane", "promise"], "must_not_re": [r"\bwife\b", r"\bbride\b"]}}},
    {"id": "en_parents_family", "language": "mn", "source_text": "Аканегийн ээж аав нь Японоос ирсэн.",
     "checks": {"tr": {"must": ["akane", "japonya"]},
                "en": {"must": ["akane", "japan"], "must_any": [["parents", "mother and father", "father and mother"]],
                       "must_not_re": [r"\bhusband\b", r"\bin-laws?\b"]}}},
    {"id": "en_bible_word_of_god", "language": "mn", "source_text": "Библи бол Бурханы үг.",
     "checks": {"tr": {"must": ["kutsal kitap", "tanrı"], "must_not": ["yehova"]},
                "en": {"must": ["bible", "god"], "must_not": ["jehovah"]}}},
    {"id": "en_chapter_only", "language": "mn", "source_text": "Эхлэл номын 2-р бүлгийг уншина.",
     "checks": {"tr": {"must": ["başlangıç", "2"], "must_not": [":"]}, "en": {"must": ["genesis", "2"], "must_not": [":"]}}},
    {"id": "en_brothers_sisters", "language": "mn", "source_text": "Ах эгч нар аа, тавтай морил.",
     "checks": {"tr": {"must": ["kardeş"]}, "en": {"must": ["brothers and sisters"], "must_not": ["siblings"]}}},
    {"id": "en_never_leaves_us", "language": "mn", "source_text": "Ехова биднийг хэзээ ч орхихгүй.",
     "checks": {"tr": {"must": ["yehova"], "negation": True}, "en": {"must": ["jehovah"], "negation": True}}},
]

# Interpreter context: a caption already shown for the Mongolian original.
REPETITION_CONTEXT = MnCandidate(
    id=1, mongolian="Гэрлэлтэд бие биенээ хайрлаж, хүндлэх нь чухал.",
    turkish="Evlilikte birbirinizi sevmek ve saygı göstermek önemlidir.", status="published",
    english="In marriage, it is important to love and respect each other.",
)


# Japanese checks (used with --japanese). Substrings of the NFKC-normalised, lower-cased caption.
JA_CHECKS: dict[str, dict] = {
    "divine_name": {"must": ["エホバ"]},
    "human_father": {"must": ["父"], "must_not": ["エホバ", "天の父", "神"]},
    "adv_father_is_not_god": {"must": ["父"], "must_not": ["エホバ", "天の父"]},
    "en_father_gives_talk": {"must": ["父"], "must_not": ["エホバ", "天の父", "神"]},
    "god_not_jehovah": {"must": ["神"], "must_not": ["エホバ"]},
    "brother_congregation": {"must_not": ["galkhuu", "新郎", "花婿"]},
    "sister_not_nurse": {"must_not": ["看護"]},
    "elder_office": {"must": ["長老"]},
    "en_elder_talk": {"must": ["長老"], "must_not": ["会長", "議長"]},
    "ephesians_alias": {"must": ["エフェソス 5:33"]},
    "ephesians_current": {"must": ["エフェソス 5:33"]},
    "first_corinthians": {"must": ["コリント第一 13:4"]},
    "genesis": {"must": ["創世記 2:24"]},
    "deuteronomy": {"must": ["申命記 6:4"]},
    "revelation": {"must": ["啓示 21:4"]},
    "negation": {"negation": True},
    "obligation_negation": {"negation": True},
    "adv_prohibition": {"negation": True},
    "en_marriage_not_contract": {"must": ["結婚"], "negation": True},
    "en_never_leaves_us": {"must": ["エホバ"], "negation": True},
    "vow_not_inferred": {"must_not": ["誓"]},
    "adv_no_vow_from_love": {"must_not": ["誓"]},
    "explicit_vow": {"must": ["誓"]},
    "invalid_reference": {"must_not": ["5:33", "5:34", "34", "三十四"]},
    "book_no_verse": {"no_digits": True},
    "prayer": {"must": ["祈"]},
    "mc_welcome": {"must_not": ["エホバ", "神", "祈"]},
    "groom_name": {"must": ["galkhuu"], "must_not": ["妻", "花嫁", "新婦", "夫婦"]},
    "akane_name": {"must_any": [["akane", "あかね", "アカネ"]], "must_not": ["夫", "花婿", "新郎", "夫婦"]},
    "japanese_independent": {"display": True},
    "en_jehovah_loves_us": {"must": ["エホバ", "愛"]},
    "en_holy_spirit": {"must": ["聖なる力"]},
    "en_jesus_christ": {"must": ["イエス"]},
    "en_husband_wife": {"must": ["夫", "妻"]},
    "en_brothers_sisters": {"must": ["兄弟姉妹"]},
    "adv_jehovah_witnesses_org": {"must": ["エホバの証人"]},
}

@dataclass
class LangCase:
    text: str = ""
    display: bool = False
    guard_ok: bool | None = None
    guard_blocked: list = field(default_factory=list)
    auto: str = "n/a"  # pass | FAIL | withheld | n/a | error
    problems: list = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    lang: str
    source: str
    relation: str | None = None
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    cache_read: int = 0
    cache_write: int = 0
    model: str = ""
    error: str | None = None
    out: dict = field(default_factory=dict)  # target -> LangCase

    # Backwards-compatible Turkish accessors (older reports / tests)
    @property
    def turkish(self) -> str:
        return self.out.get("tr", LangCase()).text

    @property
    def auto(self) -> str:
        return self.out.get("tr", LangCase()).auto


def _norm(text: str, target: str) -> str:
    if target == "ja":
        text = unicodedata.normalize("NFKC", text)
    low = turkish_lower(text) if target == "tr" else text.lower()
    return " " + low.replace("’", "'").replace("‘", "'") + " "


def check_lang(spec: dict, shared: dict, target: str, lc: LangCase, relation: str | None) -> tuple[str, list[str]]:
    probs: list[str] = []
    if "relation" in shared:
        if relation != shared["relation"]:
            probs.append(f"relation={relation} (expected {shared['relation']})")
        return ("pass" if not probs else "FAIL"), probs
    if "relation_in" in shared and relation not in shared["relation_in"]:
        probs.append(f"relation={relation} (expected one of {shared['relation_in']})")
    if not spec:
        return ("pass" if not probs else "FAIL"), probs
    if spec.get("display") and not (lc.display and lc.text):
        probs.append("not displayed")
    elif not (lc.display and lc.text):
        # Withholding is safe (never wrong) but gives the guests nothing: reported separately.
        return "withheld", probs + ["withheld (display=false)"]
    t = _norm(lc.text, target)
    for s in spec.get("must", []):
        if s not in t:
            probs.append(f"missing '{s}'")
    for alts in spec.get("must_any", []):
        if not any(a in t for a in alts):
            probs.append(f"missing one of {alts}")
    for s in spec.get("must_not", []):
        if s in t:
            probs.append(f"contains '{s.strip()}'")
    for pattern in spec.get("must_not_re", []):
        m = re.search(pattern, t)
        if m:
            probs.append(f"added '{m.group(0)}' (not in source)")
    if spec.get("negation") and not NEG[target].search(t):
        probs.append("no negation found (heuristic)")
    if spec.get("no_digits") and DIGITS.search(t):
        probs.append("contains digits")
    if spec.get("max_len") and len(lc.text) > spec["max_len"]:
        probs.append(f"too long ({len(lc.text)} chars) - possible quoted verse text")
    return ("pass" if not probs else "FAIL"), probs


def check_case(case_id: str, res, target: str = "tr", checks: dict | None = None) -> tuple[str, list[str]]:
    """Compatibility wrapper: check one language of a result object with .turkish/.display/.relation
    (or a CaseResult)."""
    spec_all = checks if checks is not None else CHECKS.get(case_id)
    if spec_all is None:
        return "n/a", []
    if isinstance(res, CaseResult):
        lc, relation = res.out.get(target, LangCase()), res.relation
    else:
        lc = LangCase(text=getattr(res, "turkish", "") if target == "tr" else getattr(res, "english", ""),
                      display=getattr(res, "display", False))
        relation = getattr(res, "relation", None)
    return check_lang(spec_all.get(target, {}), spec_all.get("*", {}), target, lc, relation)


def all_cases(glossary) -> list[dict]:
    return list(glossary.eval_cases) + EXTRA_CASES


async def run_eval(translator, glossary, case_ids: list[str] | None = None,
                   japanese: bool = False) -> list[CaseResult]:
    svc, guards = GlossaryService(glossary), Guards(glossary)
    targets = ALL_TARGETS if japanese else TARGETS
    out: list[CaseResult] = []
    for case in all_cases(glossary):
        cid = case["id"]
        if case_ids and cid not in case_ids:
            continue
        lang = case.get("language")
        r = CaseResult(cid, lang or "-", case.get("source_text", ""))
        if cid in NOT_TEXT or lang not in ("mn", "ja"):
            for t in targets:
                r.out[t] = LangCase(auto="n/a", problems=["speech-free case: covered by language-decision tests"])
            out.append(r)
            continue
        checks = dict(case.get("checks") or CHECKS.get(cid, {}))
        if japanese and "ja" not in checks and cid in JA_CHECKS:
            checks["ja"] = JA_CHECKS[cid]
        a = svc.analyze(r.source, lang)
        main = case.get("mode") == "main_speech"
        interpret = cid == "japanese_repetition" or case.get("interpret_context")
        try:
            if interpret:
                req = InterpretRequest(1, lang, r.source, "ok", a.hints, a.citations, a.citation_renderings,
                                       main_speech=main, candidates=[REPETITION_CONTEXT], renderings=a.renderings,
                                       targets=targets)
                res = await translator.interpret(req)
                r.relation = res.relation
            else:
                req = TranslateRequest(1, lang, r.source, "ok", a.hints, a.citations, a.citation_renderings,
                                       main_speech=main, renderings=a.renderings, targets=targets)
                res = await translator.translate(req)
                r.relation = "independent" if lang == "ja" else None
        except BudgetExceeded as exc:
            r.error = f"budget: {exc}"
            for t in targets:
                r.out[t] = LangCase(auto="error", problems=[r.error])
            out.append(r)
            break  # never continue past the budget stop
        except TranslatorError as exc:
            r.error = str(exc)
            for t in targets:
                r.out[t] = LangCase(auto="error", problems=[r.error])
            out.append(r)
            continue
        r.latency_ms, r.model = res.latency_ms, res.model
        r.cost_usd, r.cache_read, r.cache_write = res.usage.cost_usd, res.usage.cache_read, res.usage.cache_write
        for t in targets:
            o = res.lang(t)
            lc = LangCase(text=o.text, display=bool(o.display and not o.error))
            if o.error:
                lc.auto, lc.problems = "error", [f"language output error: {o.error}"]
                r.out[t] = lc
                continue
            if lc.display and lc.text:
                g = guards.check(r.source, lang, a.citations, o.text, target=t)
                lc.guard_ok, lc.guard_blocked, lc.text = g.ok, g.blocked, g.text
            lc.auto, lc.problems = check_lang(checks.get(t, {}), checks.get("*", {}), t, lc, r.relation)
            if lc.guard_ok is False:
                lc.problems.append("guard withheld: " + ", ".join(lc.guard_blocked))
            r.out[t] = lc
        out.append(r)
    return out


def summarize(results: list[CaseResult]) -> dict:
    lat = sorted(r.latency_ms for r in results if r.latency_ms)

    def pct(p):
        return round(lat[min(len(lat) - 1, round(p / 100 * (len(lat) - 1)))]) if lat else None

    per_lang = {}
    for t in [t for t in ALL_TARGETS if any(t in r.out for r in results)]:
        autos = [r.out.get(t, LangCase()).auto for r in results]
        per_lang[t] = {k: autos.count(k) for k in ("pass", "FAIL", "withheld", "error", "n/a")}
        per_lang[t]["guard_withheld"] = sum(r.out.get(t, LangCase()).guard_ok is False for r in results)
    return {
        "cases": len(results),
        "requests_with_output": sum(1 for r in results if r.model),
        "turkish": per_lang["tr"],
        "english": per_lang["en"],
        **({"japanese": per_lang["ja"]} if "ja" in per_lang else {}),
        "latency_p50_ms": pct(50),
        "latency_p95_ms": pct(95),
        "latency_max_ms": round(lat[-1]) if lat else None,
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "cache_read_tokens": sum(r.cache_read for r in results),
        "cache_write_tokens": sum(r.cache_write for r in results),
        "models": sorted({r.model for r in results if r.model}),
    }


def write_report(results: list[CaseResult], out_dir: Path, tag: str = "") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "")
    jl, md = out_dir / f"claude_eval_{stamp}.jsonl", out_dir / f"claude_eval_{stamp}.md"
    with open(jl, "w", encoding="utf-8") as f:
        for r in results:
            row = {k: v for k, v in r.__dict__.items() if k != "out"}
            row["out"] = {t: lc.__dict__ for t, lc in r.out.items()}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    s = summarize(results)
    lines = [f"# Claude live eval {stamp}", "", "```", json.dumps(s, ensure_ascii=False, indent=2), "```", "",
             "| case | TR | Turkish | EN | English | JA | Japanese | problems |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        tr, en, ja = r.out.get("tr", LangCase()), r.out.get("en", LangCase()), r.out.get("ja", LangCase())
        cell = lambda lc: (lc.text or ("(withheld)" if lc.auto == "withheld" else "")).replace("|", "/").replace("\n", " ")  # noqa: E731
        probs = "; ".join([f"TR {p}" for p in tr.problems] + [f"EN {p}" for p in en.problems]
                          + [f"JA {p}" for p in ja.problems]).replace("|", "/")
        lines.append(f"| {r.id} | {tr.auto} | {cell(tr)} | {en.auto} | {cell(en)} | {ja.auto} | {cell(ja)} | {probs} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jl, md
