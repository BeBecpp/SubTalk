"""Load and validate SubTalk JW Glossary v1 (read-only, loaded once at startup).

Review status ladder:
  review_required  - candidate wording; never forced (shown to Claude as optional)
  tr_attested      - Turkish wording appears in cited JW.org sources
  human_approved   - promoted by a Turkish JW reviewer via event/glossary_approvals.json
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VALID_TERM_STATUS = ("tr_attested", "review_required")
VALID_PRIORITY = ("always", "triggered", "context")


class GlossaryError(ValueError):
    pass


@dataclass(frozen=True)
class Book:
    order: int
    id: str
    mn: str
    mn_aliases: tuple[str, ...]
    ja: str
    ja_aliases: tuple[str, ...]
    tr: str
    tr_recognition_aliases: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class Term:
    id: str
    category: str
    mn: tuple[str, ...]
    ja: tuple[str, ...]
    tr: str
    status: str  # tr_attested | review_required | human_approved
    priority: str
    notes: str
    source: str  # jw_terms | wedding_terms

    @property
    def reviewed(self) -> bool:
        return self.status in ("tr_attested", "human_approved")


@dataclass(frozen=True)
class EventName:
    id: str
    mn: tuple[str, ...]
    ja: tuple[str, ...]
    tr: str
    role: str
    approved: bool


@dataclass
class Glossary:
    books: list[Book]
    terms: list[Term]
    names: list[EventName]
    policy: dict
    sources: dict
    eval_cases: list[dict]
    chapter_counts: dict[str, int]
    verse_counts: dict[str, int]
    approvals: dict = field(default_factory=dict)
    names_source: str = "example"

    def book(self, book_id: str) -> Book:
        return self._books_by_id[book_id]

    def __post_init__(self):
        self._books_by_id = {b.id: b for b in self.books}

    def summary(self) -> dict:
        by_status: dict[str, int] = {}
        for t in self.terms:
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {
            "books": len(self.books),
            "terms": len(self.terms),
            "terms_by_status": by_status,
            "names": [{"id": n.id, "tr": n.tr, "approved": n.approved} for n in self.names],
            "names_source": self.names_source,
            "approvals_reviewer": self.approvals.get("reviewer"),
            "verse_tables": len(self.verse_counts),
        }


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GlossaryError(f"Missing glossary file: {path}") from None
    except json.JSONDecodeError as exc:
        raise GlossaryError(f"Invalid JSON in {path}: {exc}") from None


def load_glossary(
    glossary_dir: Path,
    bible_dir: Path,
    *,
    event_names_path: Path | None = None,
    approvals_path: Path | None = None,
) -> Glossary:
    gdir, bdir = Path(glossary_dir), Path(bible_dir)
    errors: list[str] = []

    books_raw = _load(gdir / "bible_books.json")["books"]
    sources = _load(gdir / "sources.json")
    policy = _load(gdir / "translation_policy.json")
    eval_cases = _load(gdir / "evaluation_cases.json")["cases"]
    chapters = _load(bdir / "chapter_counts.json")["chapters"]
    verses = _load(bdir / "verse_counts.json")["verses"]

    books = []
    for b in books_raw:
        try:
            books.append(Book(
                order=int(b["order"]), id=b["id"], mn=b["mn"], mn_aliases=tuple(b.get("mn_aliases", [])),
                ja=b["ja"], ja_aliases=tuple(b.get("ja_aliases", [])), tr=b["tr"],
                tr_recognition_aliases=tuple(b.get("tr_recognition_aliases", [])), status=b.get("status", ""),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"book entry invalid: {b!r} ({exc})")
    if len(books) != 66:
        errors.append(f"expected 66 Bible books, found {len(books)}")
    if [b.order for b in books] != list(range(1, len(books) + 1)):
        errors.append("book order must be 1..66 without gaps")
    for attr in ("id", "mn", "ja", "tr"):
        values = [getattr(b, attr) for b in books]
        if len(set(values)) != len(values):
            errors.append(f"duplicate book {attr}")
        if any(not str(v).strip() for v in values):
            errors.append(f"empty book {attr}")
    missing_ch = [b.id for b in books if b.id not in chapters]
    if missing_ch:
        errors.append(f"chapter_counts missing books: {missing_ch}")

    approvals = {}
    if approvals_path and Path(approvals_path).exists():
        approvals = _load(Path(approvals_path))
    approved_ids = set(approvals.get("approved_term_ids", []))

    terms: list[Term] = []
    for fname, source in (("jw_terms.json", "jw_terms"), ("wedding_terms.json", "wedding_terms")):
        for t in _load(gdir / fname)["items"]:
            tid = t.get("id")
            if t.get("status") not in VALID_TERM_STATUS:
                errors.append(f"{tid}: invalid status {t.get('status')!r}")
            if t.get("priority") not in VALID_PRIORITY:
                errors.append(f"{tid}: invalid priority {t.get('priority')!r}")
            if not (t.get("tr") and t.get("mn") and t.get("ja")):
                errors.append(f"{tid}: tr/mn/ja must be non-empty")
            for key in t.get("source_keys", []):
                if key not in sources:
                    errors.append(f"{tid}: unknown source key {key!r}")
            status = "human_approved" if tid in approved_ids else t.get("status")
            terms.append(Term(
                id=tid, category=t.get("category", ""), mn=tuple(t.get("mn", [])), ja=tuple(t.get("ja", [])),
                tr=t.get("tr", ""), status=status, priority=t.get("priority", "context"),
                notes=t.get("notes", ""), source=source,
            ))
    ids = [b.id for b in books] + [t.id for t in terms]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"duplicate canonical IDs: {dupes}")
    unknown_approvals = approved_ids - {t.id for t in terms}
    if unknown_approvals:
        errors.append(f"approvals reference unknown term IDs: {sorted(unknown_approvals)}")

    if policy.get("bible_ref_handling", {}).get("full_verse_texts_included") is not False:
        errors.append("translation_policy must declare full_verse_texts_included: false")

    names_source = "example"
    names_path = Path(event_names_path) if event_names_path else None
    if names_path and names_path.exists():
        names_raw, names_source = _load(names_path)["entries"], "event"
    else:
        names_raw = _load(gdir / "event_names.example.json")["entries"]
    names = [
        EventName(id=n["id"], mn=tuple(n.get("mn", [])), ja=tuple(n.get("ja", [])), tr=n["tr"],
                  role=n.get("role", ""), approved=bool(n.get("approved")) and names_source == "event")
        for n in names_raw
    ]

    if errors:
        raise GlossaryError("Glossary validation failed:\n  - " + "\n  - ".join(errors))
    return Glossary(
        books=books, terms=terms, names=names, policy=policy, sources=sources, eval_cases=eval_cases,
        chapter_counts=chapters, verse_counts=verses, approvals=approvals, names_source=names_source,
    )
