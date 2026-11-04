"""Translator interface shared by the Claude and mock implementations.

One recognition event -> one TranslationResult with an independent result per
target language (Turkish 'tr', English 'en', and Japanese 'ja' when the
operator's caption mode includes it). A problem in one language never invalidates the others.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from subtalk.glossary.citations import Citation
from subtalk.glossary.service import TermHint

TARGETS = ("tr", "en")  # always requested
ALL_TARGETS = ("tr", "en", "ja")  # "ja" only when the caption mode shows Japanese

# Per-language status shown to the operator.
PASS, WITHHELD, FAILED, TIMED_OUT, STALE = "PASS", "WITHHELD", "FAILED", "TIMED_OUT", "STALE"


@dataclass
class ContextPair:
    lang: str
    source: str
    turkish: str
    english: str = ""
    japanese: str = ""


@dataclass
class MnCandidate:
    id: int
    mongolian: str
    turkish: str | None
    status: str  # published | held | translated | dropped
    english: str | None = None


@dataclass
class TranslateRequest:
    event_id: int
    lang: str  # mn | ja
    text: str
    asr_quality: str
    hints: list[TermHint]
    citations: list[Citation]
    citation_renderings: dict
    recent: list[ContextPair] = field(default_factory=list)
    main_speech: bool = False
    script_context: object | None = None  # reserved for the future script plugin (inactive)
    renderings: dict | None = None  # target -> citation key -> rendering
    targets: tuple[str, ...] = TARGETS


@dataclass
class InterpretRequest(TranslateRequest):
    candidates: list[MnCandidate] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cost_usd += other.cost_usd


@dataclass
class LangOutput:
    """Model output for one target language (before guards)."""
    display: bool
    text: str = ""
    source_quality: str = "ok"
    flags: list[str] = field(default_factory=list)
    error: str | None = None  # missing_language | timeout | api_error | refusal | invalid_json ...
    timed_out: bool = False


@dataclass
class TranslationResult:
    display: bool  # Turkish
    turkish: str
    source_quality: str = "ok"
    segment_kind: str = "speech"
    bible_mentions: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    relation: str | None = None  # interpret only
    linked_ids: list[int] = field(default_factory=list)
    conflict_note: str = ""
    model: str = ""
    latency_ms: float = 0.0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    english: str = ""
    display_en: bool | None = None  # None -> English was not produced
    source_quality_en: str | None = None
    flags_en: list[str] = field(default_factory=list)
    error_en: str | None = None
    timed_out_en: bool = False
    latency_ms_en: float | None = None
    japanese: str = ""
    display_ja: bool | None = None  # None -> Japanese was not produced (or not requested)
    source_quality_ja: str | None = None
    flags_ja: list[str] = field(default_factory=list)
    error_ja: str | None = None
    timed_out_ja: bool = False

    def lang(self, target: str) -> LangOutput:
        if target == "ja":
            if self.display_ja is None:
                return LangOutput(False, "", error=self.error_ja or "missing_language", timed_out=self.timed_out_ja)
            return LangOutput(self.display_ja, self.japanese, self.source_quality_ja or self.source_quality,
                              list(self.flags_ja), self.error_ja, self.timed_out_ja)
        if target == "en":
            if self.display_en is None:
                return LangOutput(False, "", error=self.error_en or "missing_language", timed_out=self.timed_out_en)
            return LangOutput(self.display_en, self.english, self.source_quality_en or self.source_quality,
                              list(self.flags_en), self.error_en, self.timed_out_en)
        return LangOutput(self.display, self.turkish, self.source_quality, list(self.flags), self.error)


class TranslatorError(Exception):
    pass


class TranslatorTimeout(TranslatorError):
    pass


class BudgetExceeded(TranslatorError):
    pass


class Translator(Protocol):
    name: str

    async def translate(self, req: TranslateRequest) -> TranslationResult: ...

    async def interpret(self, req: InterpretRequest) -> TranslationResult: ...

    def status(self) -> dict: ...
