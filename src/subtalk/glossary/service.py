"""Per-segment terminology selection.

Only terms whose source form actually appears in the segment are sent to
Claude (never the whole glossary, never the 66-book index). Each hint carries
the Turkish and English wording with its own review status, so unreviewed
wording is never presented as authoritative in either language.
"""
from __future__ import annotations

from dataclasses import dataclass

from subtalk.glossary.citations import Citation, CitationDetector, format_ref
from subtalk.glossary.match import contains_form
from subtalk.glossary.store import Glossary

MAX_HINTS = 14
TARGETS = ("tr", "en")
_PRIORITY_ORDER = {"always": 0, "triggered": 1, "context": 2}


@dataclass(frozen=True)
class TermHint:
    id: str
    tr: str
    status: str
    priority: str
    notes: str
    kind: str = "term"  # term | name
    en: str = ""
    en_status: str = ""
    en_notes: str = ""

    def wording(self, lang: str) -> str:
        return (self.en or self.tr) if lang == "en" else self.tr

    def label(self, lang: str = "tr") -> str:
        if self.kind == "name":
            return "NAME"
        status = self.en_status if lang == "en" else self.status
        reviewed = status in (("en_source_checked", "en_core", "human_approved") if lang == "en"
                              else ("tr_attested", "human_approved"))
        if not reviewed:
            return "CANDIDATE (unreviewed - use only if it fits naturally)"
        if self.priority == "always":
            return "REQUIRED"
        return "PREFERRED (when this meaning is intended)"

    def note(self, lang: str = "tr") -> str:
        return (self.en_notes or self.notes) if lang == "en" else self.notes


@dataclass
class SourceAnalysis:
    hints: list[TermHint]
    citations: list[Citation]
    citation_renderings: dict[tuple, str | None]  # Turkish (kept for compatibility)
    renderings: dict[str, dict[tuple, str | None]] = None  # target -> citation key -> text


class GlossaryService:
    def __init__(self, glossary: Glossary):
        self.g = glossary
        self.citations = CitationDetector(glossary)

    def select_terms(self, text: str, lang: str) -> list[TermHint]:
        hits: list[TermHint] = []
        for t in self.g.terms:
            forms = t.ja if lang == "ja" else t.mn
            if forms and contains_form(forms, text, lang):
                hits.append(TermHint(t.id, t.tr, t.status, t.priority, t.notes,
                                     en=t.en, en_status=t.en_status, en_notes=t.en_notes))
        for n in self.g.names:
            forms = n.ja if lang == "ja" else n.mn
            if forms and contains_form(forms, text, lang):
                # Approval status is for the operator (guard flag), not for Claude: an
                # "unapproved" note invited the model to re-spell the name. The role (groom/bride)
                # is not sent either - it could invite "damat"/"gelin" that were not spoken.
                en = n.target("en")
                hits.append(TermHint(n.id, n.tr, "approved" if n.approved else "unapproved", "always",
                                     f"use exactly '{n.tr}'", "name", en=en, en_status="name",
                                     en_notes=f"use exactly '{en}'"))
        hits.sort(key=lambda h: (h.kind != "name", _PRIORITY_ORDER.get(h.priority, 3), h.status == "review_required"))
        return hits[:MAX_HINTS]

    def analyze(self, text: str, lang: str) -> SourceAnalysis:
        cites = self.citations.detect(text, lang)
        renderings = {t: {c.key(): format_ref(self.g, c, t) for c in cites} for t in TARGETS}
        return SourceAnalysis(
            hints=self.select_terms(text, lang),
            citations=cites,
            citation_renderings=renderings["tr"],
            renderings=renderings,
        )
