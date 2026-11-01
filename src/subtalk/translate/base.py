"""Translator interface shared by the Claude and mock implementations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from subtalk.glossary.citations import Citation
from subtalk.glossary.service import TermHint


@dataclass
class ContextPair:
    lang: str
    source: str
    turkish: str


@dataclass
class MnCandidate:
    id: int
    mongolian: str
    turkish: str | None
    status: str  # published | held | translated | dropped


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


@dataclass
class TranslationResult:
    display: bool
    turkish: str
    source_quality: str = "ok"  # ok | degraded | garbled
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


class TranslatorError(Exception):
    pass


class BudgetExceeded(TranslatorError):
    pass


class Translator(Protocol):
    name: str

    async def translate(self, req: TranslateRequest) -> TranslationResult: ...

    async def interpret(self, req: InterpretRequest) -> TranslationResult: ...

    def status(self) -> dict: ...
