"""Compose the Zoom caption payload(s) for one speech event from its per-language texts.

Zoom's Manual Caption API is a single caption stream (no documented per-attendee
language channel), so one event becomes ONE sequence of POSTs:

* TR        - Turkish only (nothing is sent if Turkish was withheld)
* EN        - English only
* BILINGUAL - Turkish and English lines in the same POST ("TR: ...\\nEN: ...");
              if one language was withheld, only the other is sent.

Long texts are chunked per language and paired chunk by chunk.
"""
from __future__ import annotations

from subtalk.output.chunker import chunk_caption

MODES = ("TR", "EN", "BILINGUAL")


def compose_parts(texts: dict[str, str], mode: str, max_chars: int = 84, labels: bool = True) -> list[str]:
    tr = (texts.get("tr") or "").strip()
    en = (texts.get("en") or "").strip()
    if mode == "TR":
        return chunk_caption(tr, max_chars) if tr else []
    if mode == "EN":
        return chunk_caption(en, max_chars) if en else []
    if mode != "BILINGUAL":
        raise ValueError(f"unknown output mode {mode!r}")
    tr_parts = chunk_caption(tr, max_chars) if tr else []
    en_parts = chunk_caption(en, max_chars) if en else []
    out = []
    for i in range(max(len(tr_parts), len(en_parts))):
        lines = []
        if i < len(tr_parts):
            lines.append(("TR: " if labels else "") + tr_parts[i])
        if i < len(en_parts):
            lines.append(("EN: " if labels else "") + en_parts[i])
        out.append("\n".join(lines))
    return out
