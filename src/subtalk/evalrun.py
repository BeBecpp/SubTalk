"""Live Claude evaluation on the JW Glossary v1 evaluation cases.

Runs every text case through the production path (glossary analysis ->
Claude -> deterministic guards) and applies an automatic check per case.
Automatic checks are heuristics that catch clear failures; a Turkish
speaker still has to read the output. Results: console table plus
bench_out/claude_eval_<time>.jsonl and .md.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from subtalk.glossary.guards import Guards
from subtalk.glossary.normalize import turkish_lower
from subtalk.glossary.service import GlossaryService
from subtalk.translate.base import InterpretRequest, MnCandidate, TranslateRequest, TranslatorError

# Turkish negation (heuristic): negative verb suffixes, değil, yok, gerekmez/olmaz.
NEG = re.compile(
    r"\w+(m[ae]d[ıiuü]|m[ae]m[ıiuü]ş|m[ae]z\b|m[ae]y[ıiuü]|m[ae]l[ıi]|m[ae]yece|m[ae]yaca)\w*|\bdeğil|\byok\b|\bgerekmez|\bolmaz|\basla\b|\bhiç\b"
)
DIGITS = re.compile(r"\d")

# case id -> check spec. must/must_not are lower-cased Turkish substrings.
CHECKS: dict[str, dict] = {
    "divine_name": {"must": ["yehova"]},
    "human_father": {"must": ["baba"], "must_not": ["tanrı", "yehova", "rab "]},
    "god_not_jehovah": {"must": ["tanrı"], "must_not": ["yehova"]},
    "brother_family": {"must": ["ağabey"], "must_not": ["birader"]},
    "brother_congregation": {"must_not": ["galkhuu", "damat"]},
    "sister_not_nurse": {"must_not": ["hemşire"]},
    "elder_office": {"must": ["ihtiyar"]},
    "ephesians_alias": {"must": ["efesliler 5:33"], "must_not": ["efesoslular"]},
    "ephesians_current": {"must": ["efesliler 5:33"]},
    "first_corinthians": {"must": ["1. korintliler 13:4"], "must_not": ["korintoslular"]},
    "genesis": {"must": ["başlangıç 2:24"]},
    "deuteronomy": {"must": ["kanun'un tekrarı 6:4"]},
    "revelation": {"must": ["vahiy 21:4"]},
    "negation": {"negation": True},
    "obligation_negation": {"negation": True},
    "vow_not_inferred": {"must_not": ["yemin"]},
    "explicit_vow": {"must": ["yemin"]},
    "bible_explanation": {"negation": True, "no_digits": True},
    "invalid_reference": {"must_not": ["5:33", "5:34"]},
    "book_no_verse": {"no_digits": True},
    "prayer": {"must": ["dua"]},
    "title_reference": {"must": ["kutsal kitap"], "no_digits": True},
    "mc_welcome": {"must_not": ["yehova", "tanrı", "dua"]},
    "groom_name": {"must": ["galkhuu"], "must_not_re": [r"\beş\w*", r"\bkarı\w*", r"\bgelin\b", r"\bçift\w*"]},
    "akane_name": {"must": ["akane"], "must_not_re": [r"\beş\w*", r"\bkoca\w*", r"\bdama[dt]\w*", r"\bçift\w*"]},
    "japanese_independent": {"display": True},
    "japanese_repetition": {"relation": "covered"},
    "plain_contract": {"must_not": ["ahit", "ahd"]},
    "numerical_difference": {"must_any": [["30", "otuz"], ["10", " on "]], "negation": True},
}
# Speech-free cases belong to the ASR layer (tests/test_langid.py), not translation.
NOT_TEXT = {"music_only", "applause_only"}

# For the repetition case the interpreter repeats an already shown caption.
REPETITION_CONTEXT = MnCandidate(
    id=1, mongolian="Гэрлэлтэд бие биенээ хайрлаж, хүндлэх нь чухал.",
    turkish="Evlilikte birbirinizi sevmek ve saygı göstermek önemlidir.", status="published",
)


@dataclass
class CaseResult:
    id: str
    lang: str
    source: str
    turkish: str = ""
    display: bool = False
    relation: str | None = None
    guard_ok: bool | None = None
    guard_blocked: list = field(default_factory=list)
    auto: str = "n/a"  # pass | FAIL | withheld | n/a | error
    problems: list = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    cache_read: int = 0
    model: str = ""
    error: str | None = None


def _norm(text: str) -> str:
    return turkish_lower(text).replace("’", "'").replace("‘", "'")


def check_case(case_id: str, res: CaseResult) -> tuple[str, list[str]]:
    spec = CHECKS.get(case_id)
    if spec is None:
        return "n/a", []
    probs: list[str] = []
    t = " " + _norm(res.turkish) + " "
    if "relation" in spec:
        if res.relation != spec["relation"]:
            probs.append(f"relation={res.relation} (expected {spec['relation']})")
        return ("pass" if not probs else "FAIL"), probs
    if spec.get("display") and not (res.display and res.turkish):
        probs.append("not displayed")
    elif not (res.display and res.turkish):
        # Withholding is safe (never wrong) but gives the guests nothing: reported separately.
        return "withheld", ["Claude withheld the caption (display=false)"]
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
    if spec.get("negation") and not NEG.search(t):
        probs.append("no negation found (heuristic)")
    if spec.get("no_digits") and DIGITS.search(t):
        probs.append("contains digits")
    return ("pass" if not probs else "FAIL"), probs


async def run_eval(translator, glossary, case_ids: list[str] | None = None) -> list[CaseResult]:
    svc, guards = GlossaryService(glossary), Guards(glossary)
    out: list[CaseResult] = []
    for case in glossary.eval_cases:
        cid = case["id"]
        if case_ids and cid not in case_ids:
            continue
        lang = case.get("language")
        r = CaseResult(cid, lang or "-", case.get("source_text", ""))
        if cid in NOT_TEXT or lang not in ("mn", "ja"):
            r.auto = "n/a"
            r.problems = ["speech-free case: covered by language-decision tests"]
            out.append(r)
            continue
        a = svc.analyze(r.source, lang)
        main = case.get("mode") == "main_speech"
        try:
            if cid.startswith("japanese_"):
                cands = [REPETITION_CONTEXT] if cid == "japanese_repetition" else []
                req = InterpretRequest(1, lang, r.source, "ok", a.hints, a.citations, a.citation_renderings,
                                       main_speech=main, candidates=cands)
                res = await (translator.interpret(req) if cands else translator.translate(req))
                r.relation = res.relation if cands else "independent"
            else:
                req = TranslateRequest(1, lang, r.source, "ok", a.hints, a.citations, a.citation_renderings,
                                       main_speech=main)
                res = await translator.translate(req)
        except TranslatorError as exc:
            r.error, r.auto = str(exc), "error"
            out.append(r)
            continue
        r.turkish, r.display = res.turkish, res.display
        r.latency_ms, r.model = res.latency_ms, res.model
        r.cost_usd, r.cache_read = res.usage.cost_usd, res.usage.cache_read
        if res.display and res.turkish:
            g = guards.check(r.source, lang, a.citations, res.turkish)
            r.guard_ok, r.guard_blocked = g.ok, g.blocked
            r.turkish = g.text
        r.auto, r.problems = check_case(cid, r)
        if r.guard_ok is False:
            r.problems.append("guard would withhold: " + ", ".join(r.guard_blocked))
        out.append(r)
    return out


def summarize(results: list[CaseResult]) -> dict:
    lat = sorted(r.latency_ms for r in results if r.latency_ms)
    pct = lambda p: lat[min(len(lat) - 1, round(p / 100 * (len(lat) - 1)))] if lat else None  # noqa: E731
    return {
        "cases": len(results),
        "pass": sum(r.auto == "pass" for r in results),
        "fail": sum(r.auto == "FAIL" for r in results),
        "withheld": sum(r.auto == "withheld" for r in results),
        "error": sum(r.auto == "error" for r in results),
        "n_a": sum(r.auto == "n/a" for r in results),
        "guard_withheld": sum(r.guard_ok is False for r in results),
        "latency_p50_ms": round(pct(50)) if lat else None,
        "latency_p95_ms": round(pct(95)) if lat else None,
        "latency_max_ms": round(lat[-1]) if lat else None,
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "cache_read_tokens": sum(r.cache_read for r in results),
        "models": sorted({r.model for r in results if r.model}),
    }


def write_report(results: list[CaseResult], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jl, md = out_dir / f"claude_eval_{stamp}.jsonl", out_dir / f"claude_eval_{stamp}.md"
    with open(jl, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
    s = summarize(results)
    lines = [f"# Claude live eval {stamp}", "", "```", json.dumps(s, ensure_ascii=False, indent=2), "```", "",
             "| case | auto | Turkish | problems |", "|---|---|---|---|"]
    for r in results:
        tr = (r.turkish or ("(withheld)" if not r.display else "")).replace("|", "/")
        lines.append(f"| {r.id} | {r.auto} | {tr} | {'; '.join(r.problems).replace('|', '/')} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jl, md
