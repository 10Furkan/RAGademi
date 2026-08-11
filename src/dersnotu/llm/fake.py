"""Deterministic offline client that exercises the complete pipeline."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ..models import Usage
from .client import CallResult

_SECTION_RE = re.compile(r"# SECTION TO WRITE NOW: (.*)")
_SLIDES_RE = re.compile(r"Slides (\d+)[–-](\d+)")
_CITE_RE = re.compile(r"### \[B: ([^\]]+)\]\n(.{0,400})", re.S)
_SLIDE_TITLE_RE = re.compile(r"--- Slide (\d+): ([^\n-]*) ---")
_BOOK_FIG_RE = re.compile(r"^- `([\d.]+)` — (.*?) \(p\. (\d+)\)$", re.M)
_PRACTICE_SLIDE_RE = re.compile(r"^### Slide (\d+): (.*)$", re.M)
_EXAM_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(.{25,180}\?)\s*$", re.M)


class FakeLLMClient:
    """Provide deterministic responses without network access or cost."""

    def __init__(
        self,
        model: str = "fake",
        *,
        cheap_model: str = "fake",
        effort: str = "high",
        max_tokens: int = 16000,
        delay: float = 0.012,
    ):
        self.model = model
        self.cheap_model = cheap_model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        self.delay = delay

    def complete(
        self,
        *,
        system: str | list[dict],
        content: list[dict],
        model: str | None = None,
        json_schema: dict | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallResult:
        text_blocks = [block.get("text", "") for block in content if block.get("type") == "text"]
        joined = "\n".join(text_blocks)
        images = sum(1 for block in content if block.get("type") == "image")
        body = self._compose(joined, images)
        if on_delta:
            for chunk in _chunks(body, 90):
                on_delta(chunk)
                if self.delay:
                    time.sleep(self.delay)
        usage = Usage(
            input_tokens=len(joined) // 4 + images * 1960,
            output_tokens=len(body) // 4,
            calls=1,
        )
        self.usage.add(usage)
        return CallResult(text=body, usage=usage, stop_reason="end_turn")

    def complete_json(
        self, *, system: str, content: list[dict], schema: dict, model: str | None = None
    ) -> Any:
        joined = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
        self.usage.add(Usage(input_tokens=len(joined) // 4, output_tokens=300, calls=1))
        if "cards" in str(schema):
            return {"cards": self._fake_cards(joined)}
        if "modeled_on" in str(schema):
            return self._fake_exam(joined)
        return {"alignments": self._fake_alignments(joined)}

    def _fake_cards(self, payload: str) -> list[dict]:
        cards = []
        for match in re.finditer(r"## Section (\d+) \(slides (\d+)-(\d+)\)", payload):
            index = int(match.group(1))
            titles = _SLIDE_TITLE_RE.findall(payload[match.end() : match.end() + 1200])
            title = titles[0][1].strip() if titles else f"Section {index + 1}"
            cards.append(
                {
                    "section_index": index,
                    "title": title or f"Section {index + 1}",
                    "key_terms": [item[1].strip() for item in titles[:6] if item[1].strip()],
                    "formulas": [],
                    "gaps": [],
                }
            )
        return cards

    def _fake_exam(self, payload: str) -> dict:
        titles = [(number, title.strip()) for number, title in _PRACTICE_SLIDE_RE.findall(payload) if title.strip()]
        citations = [citation for citation, _ in _CITE_RE.findall(payload)]
        format_start = payload.find("## FORMAT")
        source_questions = _EXAM_LINE_RE.findall(payload[format_start:] if format_start >= 0 else "")
        requested = re.search(r"Generate exactly (\d+) questions", payload)
        count = int(requested.group(1)) if requested else 4
        count = max(1, min(count, len(titles) or 1, 12))

        questions = []
        for index in range(count):
            slide, title = titles[index % len(titles)] if titles else ("?", "Topic")
            citation = citations[index % len(citations)] if citations else ""
            multiple_choice = index % 2 == 0
            questions.append(
                {
                    "number": index + 1,
                    "kind": "multiple choice" if multiple_choice else "calculation",
                    "points": 10,
                    "topic": title,
                    "slides": [int(slide)] if str(slide).isdigit() else [],
                    "prompt": (
                        f"**Demo question.** Within slide {slide}'s coverage of “{title}”: "
                        "What value does $x = 0\\text{xCA}$ represent as an 8-bit signed integer?"
                    ),
                    "choices": ["$-54$", "$202$", "$-53$", "$54$"] if multiple_choice else [],
                    "answer": "A" if multiple_choice else "$-54$",
                    "solution": (
                        "`0xCA` = `11001010`. Because the most significant bit is 1, "
                        "the value is negative: $-2^7 + 2^6 + 2^3 + 2^1 = -54$.\n\n"
                        "The `202` distractor treats it as unsigned; `-53` reflects an "
                        "off-by-one error in two's-complement conversion.\n\n"
                        "```c\nsigned char x = 0xCA;   /* -54 */\n```"
                    ),
                    "citations": [citation] if citation else [],
                    "modeled_on": source_questions[index % len(source_questions)].strip() if source_questions else "",
                }
            )
        return {
            "profile": (
                "Demo profile: the fake client read the paper and built questions from "
                "real slide titles and textbook excerpts. No model was called."
            ),
            "duration_minutes": 60,
            "questions": questions,
        }

    def _fake_alignments(self, payload: str) -> list[dict]:
        return [
            {
                "section_index": int(match.group(1)),
                "book_sections": [],
                "page_start": 0,
                "page_end": 0,
                "confidence": "low",
            }
            for match in re.finditer(r"Section (\d+):", payload)
        ]

    def _compose(self, payload: str, images: int) -> str:
        match = _SECTION_RE.search(payload)
        title = match.group(1).strip() if match else "Section"
        slide_range = _SLIDES_RE.search(payload)
        first, last = slide_range.groups() if slide_range else ("?", "?")
        slide_titles = _SLIDE_TITLE_RE.findall(payload)
        citations = _CITE_RE.findall(payload)
        parts = [
            f"## {title}",
            "",
            "> ℹ️ **Demo output.** This text comes from the deterministic fake client, "
            "not a live model. The slide titles and textbook citations below are real.",
            "",
            f"This section covers slides {first}–{last}"
            + (f" and supplied {images} slide images." if images else "."),
            "",
        ]
        if slide_titles:
            parts += ["### Topics covered in the slides", ""]
            parts += [f"- **{heading.strip()}** [S: {number}]" for number, heading in slide_titles if heading.strip()]
            parts.append("")

        book_figures = _BOOK_FIG_RE.findall(payload)
        if book_figures:
            number, caption, page = book_figures[0]
            parts += [
                "### Textbook diagram",
                "",
                f"The following figure summarizes the topic: {caption}. [B: p. {page}]",
                "",
                f"[BOOKFIGURE: {number}]",
                "",
            ]
        if citations:
            parts += ["### Textbook evidence", ""]
            for citation, excerpt in citations[:4]:
                parts += [f"{' '.join(excerpt.split())[:260]}… [B: {citation}]", ""]
        else:
            parts += ["> ⚠️ No matching textbook excerpt was found for this section.", ""]
        parts += [
            "### Format check",
            "",
            "Inline mathematics $x_{w-1}$ and display mathematics:",
            "",
            "$$B2U_w(\\vec{x}) = \\sum_{i=0}^{w-1} x_i \\cdot 2^i$$",
            "",
            "```c",
            "unsigned char a = 0x69;   /* 01101001 */",
            "```",
            "",
        ]
        parts += _requested_callouts(payload)
        return "\n".join(parts)


def _requested_callouts(payload: str) -> list[str]:
    """Mirror requested callout blocks so prompt and renderer are tested together."""
    out: list[str] = []
    if "::: analogy" in payload:
        out += [
            "::: analogy",
            "Think of a byte as a box of eight switches: each is either on or off.",
            "",
            "**Where it breaks down:** Switch positions carry different bit weights.",
            ":::"
            "",
        ]
    if "::: quiz" in payload:
        out += [
            "::: quiz",
            "1. Convert `0xCA` to binary.",
            "2. What value does `0xFF` represent as an 8-bit signed integer?",
            "",
            "**Answers:** 1) `11001010` 2) $-1$",
            ":::"
            "",
        ]
    if "::: glossary" in payload:
        out += [
            "::: glossary",
            "| Term | Meaning |",
            "|---|---|",
            "| Byte | Group of eight bits |",
            "| Two's complement | Signed integer representation |",
            ":::"
            "",
        ]
    return out


def _chunks(text: str, size: int):
    for index in range(0, len(text), size):
        yield text[index : index + size]
