"""Per-segment terminology selection.

Only terms whose source form actually appears in the segment are sent to
Claude (never the whole glossary, never the 66-book index). Each hint is
labelled by review status so unreviewed wording is never presented as
authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass

from subtalk.glossary.citations import Citation, CitationDetector, format_tr
from subtalk.glossary.match import contains_form
from subtalk.glossary.store import Glossary

MAX_HINTS = 14
_PRIORITY_ORDER = {"always": 0, "triggered": 1, "context": 2}


@dataclass(frozen=True)
class TermHint:
    id: str
    tr: str
    status: str
    priority: str
    notes: str
    kind: str = "term"  # term | name

    def label(self) -> str:
        if self.kind == "name":
            return "NAME"
        if self.status == "review_required":
            return "CANDIDATE (unreviewed - use only if it fits naturally)"
        if self.priority == "always":
            return "REQUIRED"
        return "PREFERRED (when this meaning is intended)"


@dataclass
class SourceAnalysis:
    hints: list[TermHint]
    citations: list[Citation]
    citation_renderings: dict[tuple, str | None]


class GlossaryService:
    def __init__(self, glossary: Glossary):
        self.g = glossary
        self.citations = CitationDetector(glossary)

    def select_terms(self, text: str, lang: str) -> list[TermHint]:
        hits: list[TermHint] = []
        for t in self.g.terms:
            forms = t.ja if lang == "ja" else t.mn
            if forms and contains_form(forms, text, lang):
                hits.append(TermHint(t.id, t.tr, t.status, t.priority, t.notes))
        for n in self.g.names:
            forms = n.ja if lang == "ja" else n.mn
            if forms and contains_form(forms, text, lang):
                note = "" if n.approved else "Latin spelling not yet approved by the family"
                hits.append(TermHint(n.id, n.tr, "approved" if n.approved else "unapproved", "always", note, "name"))
        hits.sort(key=lambda h: (h.kind != "name", _PRIORITY_ORDER.get(h.priority, 3), h.status == "review_required"))
        return hits[:MAX_HINTS]

    def analyze(self, text: str, lang: str) -> SourceAnalysis:
        cites = self.citations.detect(text, lang)
        return SourceAnalysis(
            hints=self.select_terms(text, lang),
            citations=cites,
            citation_renderings={c.key(): format_tr(self.g, c) for c in cites},
        )
