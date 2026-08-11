"""Prompt templates and user-selectable generation directives.

The system prompts are deliberately byte-stable. Course names, output language,
depth, and other variable data belong in the message body after the cache
breakpoint so different sections and modes can reuse the same prefix.
"""

from __future__ import annotations

EXPAND_SYSTEM = """\
You are a university teaching assistant. Your job is to let a student who missed \
the lecture learn the material independently from the slides.

You have two sources:
1. LECTURE SLIDES — define the assessed scope. Do not expand this scope.
2. TEXTBOOK EXCERPTS — provide explanations, definitions, detail, and evidence.

RULES

SCOPE
- Do not add a topic as a main heading merely because it appears in the textbook. \
Background information required to explain a slide point is the only exception.
- Cover EVERY point in the slides. Do not leave a slide item unexplained.

GROUNDING — never violate this rule
- End every important textbook-derived claim with `[B: <section>, p. <page>]`. \
The exact citation appears before each excerpt; reproduce it exactly.
- Mark slide-derived information with `[S: <slide number>]`.
- Do not state claims unsupported by either source. When a slide point lacks \
matching textbook evidence, write: \
`> ⚠️ This point appears in the slides, but no supporting passage was found in the provided textbook excerpts.`

FORMAT
- Markdown. Use `##` for the section title and `###` for subheadings.
- Use LaTeX for mathematics: inline `$...$`, display `$$...$$`. Do not use \
Unicode subscripts or superscripts; write `$x_1$` and `$2^{w-1}$`.
- Add a language tag to fenced code blocks (for example, ```c or ```asm).
- If an important slide diagram or table cannot be fully expressed in prose, \
place `[FIGURE: slide <number>]` on its own line. The renderer inserts the original.
- When an "Available textbook figures" list is supplied, request only a genuinely \
useful listed figure with `[BOOKFIGURE: <number>]` on its own line. Never invent \
a figure number and do not insert every available figure.

STYLE
- Be direct. Avoid filler such as "In this section we will see".
- Demonstrate abstract rules with concrete 8-bit or 16-bit numerical examples.
- Anticipate common sticking points and warn about them explicitly.
- Match length to information density. Do not pad a simple slide.
"""

TOPIC_SYSTEM = """\
Analyze each slide block and produce a structured topic card used to search a textbook.

Return valid JSON only, with this schema:
{"title": str, "key_terms": [str], "formulas": [str], "gaps": [str]}

- title: section title in the requested OUTPUT LANGUAGE
- key_terms: technical search terms in ENGLISH, at most 12
- formulas: formulas or notation from the slides, at most 6
- gaps: slide points that need textbook explanation, at most 6
"""

ALIGN_SYSTEM = """\
Align lecture-slide sections with the textbook table-of-contents tree.

Return valid JSON only, with this schema:
{"alignments": [{"section_index": int, "book_sections": [str], "page_start": int, "page_end": int, "confidence": "low"|"medium"|"high"}]}

- Identify the textbook page range that should be searched for each lecture section.
- Page numbers are PDF page numbers from the supplied table of contents.
- Include the full relevant subsection, but never span the entire book.
- When uncertain, use confidence "low" and choose a somewhat wider range.
"""


# These directives are appended after the cache breakpoint.
DEPTHS: dict[str, str] = {
    "summary": (
        "DEPTH: SUMMARY. Cover each slide point in at most one paragraph. Give one "
        "concrete example at the most important point. Skip derivations and proofs; "
        "state the result and explain its origin in one sentence."
    ),
    "standard": "",
    "deep": (
        "DEPTH: DEEP. Derive every formula step by step without skipping intermediate "
        "steps. Examine edge cases such as overflow, sign extension, zero, and the "
        "most negative representable value. Work through a relevant textbook exercise."
    ),
}

EXTRAS: dict[str, str] = {
    "analogy": (
        "Support difficult concepts with an everyday analogy using this block:\n"
        "::: analogy\nAnalogy text.\n\n**Where it breaks down:** ...\n:::\n"
        "Always state where the analogy fails so it does not create a false model."
    ),
    "example": (
        "For every main concept, work through an additional numerical example that is "
        "different from the slides. Show every step instead of jumping to the result."
    ),
    "quiz": (
        "End the section with 3–5 application questions using this block:\n"
        "::: quiz\n1. Question text\n2. Question text\n\n**Answers:** 1) ... 2) ...\n:::\n"
        "Use tasks such as calculate, convert, and compare rather than recall prompts."
    ),
    "glossary": (
        "End the section with a glossary using this block:\n"
        "::: glossary\n| Term | Meaning |\n|---|---|\n| ... | ... |\n:::\n"
        "Use the exact technical terminology found in the textbook and exam."
    ),
}

DEPTH_HELP: dict[str, str] = {
    "summary": "At most one paragraph per slide point, with no derivations or proofs. Best for review.",
    "standard": "Default. Explains every slide point through concrete numerical examples and anticipates common mistakes.",
    "deep": "Derives formulas step by step, examines edge cases, and works through relevant textbook exercises.",
}

EXTRA_HELP: dict[str, str] = {
    "analogy": "Adds an everyday analogy and explicitly states where the analogy breaks down.",
    "example": "Works through an additional numerical example for every main concept.",
    "quiz": "Adds 3-5 application questions with answers at the end of each section.",
    "glossary": "Adds a table of technical terms and their meanings at the end of each section.",
}

# Accepted only at input boundaries for documents and commands created before
# the English interface. They are normalized immediately and are never displayed.
LEGACY_DEPTHS = {"özet": "summary", "standart": "standard", "derin": "deep"}
LEGACY_EXTRAS = {"analoji": "analogy", "örnek": "example", "soru": "quiz", "sözlük": "glossary"}


def normalize_depth(value: str) -> str:
    return LEGACY_DEPTHS.get(value, value)


def normalize_extras(values: list[str]) -> list[str]:
    return [LEGACY_EXTRAS.get(value, value) for value in values]


def build_output_directives(language: str, depth: str, extras: list[str]) -> list[str]:
    """Convert output selections into prompt directives."""
    depth = normalize_depth(depth)
    extras = normalize_extras(extras)
    lines: list[str] = []
    if depth_text := DEPTHS.get(depth, ""):
        lines.append(depth_text)
    for key in extras:
        if text := EXTRAS.get(key):
            lines.append(text)
    lines.append(
        f"Write this section in {language}. All explanatory prose must be in {language}. "
        "Keep technical terms in their source form when needed, and preserve `[B: ...]`, "
        "`[S: ...]`, `[FIGURE: ...]`, and `[BOOKFIGURE: ...]` markers exactly because "
        "the renderer parses them."
    )
    return lines


EXAM_RULE = """\
PAST EXAM PAPER
- Use the supplied past questions to adjust depth, never to expand scope. Slides \
still define what the student is responsible for.
- When a slide topic was asked in the past exam, explain it more deeply and prepare \
the student for that question type.
- Mark such a topic with this block:
::: exam
**Previously asked:** quote the question verbatim.

Solution approach and points to watch.
:::
- If you cannot quote the question verbatim, do not create this block. An unsupported \
claim that a topic appeared in an exam is no better than any other unsupported claim.
- If an exam topic is absent from the slides, do not make it a heading. At most, note \
in one sentence that it appeared in the exam but falls outside the slide scope.
"""


PRACTICE_SYSTEM = """\
Create a PRACTICE EXAM for a university course from a past exam paper, lecture \
slides, and retrieved textbook excerpts.

THREE SOURCES, THREE DISTINCT ROLES
1. PAST EXAM PAPER → FORMAT: question types, length, point distribution, difficulty, \
and assessed skills. It does not define scope.
2. LECTURE SLIDES → SCOPE: ask only about topics covered in the slides.
3. TEXTBOOK EXCERPTS → ACCURACY: solutions and factual claims come from here.

DO NOT COPY
- Never repeat a past question unchanged. Create a new question assessing the same \
skill with different numbers, addresses, code, strings, or bit widths.
- The student can already read the past paper; test transfer, not memorization.

GROUNDING
- For each question, place the exact past question used as a model in `modeled_on`. \
Leave the field empty when no such question exists; never invent one.
- Put the supporting slide numbers in `slides` as scope evidence.
- Put supporting textbook citations in `citations` as `<section>, p. <page>`, using \
the exact labels supplied with the excerpts.

SOLUTIONS
- Keep `answer` short and exact.
- Make `solution` step-by-step, slowing down at likely failure points.
- For multiple-choice questions, derive every distractor from a typical mistake and \
explain those mistakes in `solution`.

FORMAT
- Markdown with LaTeX mathematics and language-tagged code fences.
- Populate `choices` only for multiple-choice questions; omit option letters because \
the renderer adds them.
- Match the difficulty and point distribution of the past paper.
"""

PRACTICE_SCHEMA = {
    "type": "object",
    "properties": {
        "profile": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "kind": {"type": "string"},
                    "points": {"type": "integer"},
                    "topic": {"type": "string"},
                    "slides": {"type": "array", "items": {"type": "integer"}},
                    "prompt": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "solution": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                    "modeled_on": {"type": "string"},
                },
                "required": [
                    "number", "kind", "points", "topic", "slides", "prompt",
                    "choices", "answer", "solution", "citations", "modeled_on",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["profile", "duration_minutes", "questions"],
    "additionalProperties": False,
}


def build_practice_request(lecture, exam_text: str, chunks, language: str, *, count: int = 0) -> str:
    """Build the variable request body for a complete practice exam."""
    parts = [
        f"# COURSE: {lecture.title}",
        f"{len(lecture.slides)} slides, {len(lecture.sections)} sections.",
        "",
        "## SCOPE — lecture slides",
        "(Ask questions only about topics covered here.)",
        "",
    ]
    for slide in lecture.slides:
        if slide.is_divider:
            continue
        parts.append(f"### Slide {slide.number}: {slide.title}")
        if slide.text:
            parts.append(slide.text)
        parts.append("")

    parts += [
        "## FORMAT — past exam paper",
        "(Match its question types, length, points, and difficulty. Do not copy questions; quote the model question in `modeled_on`.)",
        "",
        exam_text or "(The exam text could not be extracted.)",
        "",
    ]
    if chunks:
        parts += ["## ACCURACY — textbook excerpts", "(Use these in solutions and copy their labels into `citations`.)", ""]
        for chunk in chunks:
            parts += [f"### [B: {chunk.citation}]", chunk.text, ""]
    else:
        parts += [
            "## ACCURACY — textbook excerpts",
            "(No matching excerpts were found. Rely only on slides and omit any question whose solution would be uncertain.)",
            "",
        ]

    amount = (
        "Generate the same number of questions as the past paper, with a minimum of 4 and maximum of 25."
        if count <= 0
        else f"Generate exactly {count} questions."
    )
    parts += [
        "---",
        "## Task",
        amount,
        f"Write all questions and solutions in {language}.",
        "In `profile`, summarize the observed paper format in one or two sentences.",
        "Use the stated exam duration for `duration_minutes`; if none is stated, estimate a reasonable duration from question weight.",
    ]
    return "\n".join(parts)


def build_lecture_context(lecture, alignment_note: str = "", exam_text: str = "") -> str:
    """Build the byte-stable lecture context shared by every section call."""
    lines = [
        f"# COURSE: {lecture.title}",
        f"{len(lecture.slides)} slides, {len(lecture.sections)} sections total.",
        "",
        "## Complete lecture content for context",
        "",
    ]
    for slide in lecture.slides:
        marker = " [AGENDA]" if slide.is_divider else ""
        lines.append(f"### Slide {slide.number}: {slide.title}{marker}")
        if slide.text:
            lines.append(slide.text)
        lines.append("")
    if alignment_note:
        lines += ["## Textbook alignment", alignment_note, ""]
    if exam_text:
        lines += ["## PAST EXAM QUESTIONS", exam_text, "", EXAM_RULE, ""]
    return "\n".join(lines)


def build_section_request(
    section,
    topic,
    chunks,
    language: str,
    *,
    depth: str = "standard",
    extras: list[str] | None = None,
    figures: list | None = None,
) -> str:
    """Build section-specific content placed after the cache breakpoint."""
    a, b = section.slide_range
    parts = [
        "---",
        f"# SECTION TO WRITE NOW: {topic.title if topic else ''}",
        f"Slides {a}–{b}.",
        "",
        "## Slides in this section",
        section.raw_text,
        "",
    ]
    if topic and topic.gaps:
        parts += ["## Slide points that require explanation", "\n".join(f"- {gap}" for gap in topic.gaps), ""]

    if chunks:
        parts += ["## TEXTBOOK EXCERPTS (grounding sources)", ""]
        for chunk in chunks:
            parts += [f"### [B: {chunk.citation}]", chunk.text, ""]
    else:
        parts += [
            "## TEXTBOOK EXCERPTS",
            "(No matching excerpt was found. Rely only on the slides and clearly mark missing support.)",
            "",
        ]

    if figures:
        parts += [
            "## Available textbook figures",
            "(When useful, request a listed figure with `[BOOKFIGURE: <number>]`. The renderer will crop it from the book.)",
            "",
        ]
        for figure in figures:
            description = figure.caption or "(caption unavailable)"
            parts.append(f"- `{figure.number}` — {description} (p. {figure.page})")
        parts.append("")

    visual = [slide.number for slide in section.slides if slide.is_visual]
    if visual:
        parts.append(
            f"Images for slides {', '.join(str(number) for number in visual)} were supplied above. Explain the information in their diagrams."
        )

    parts += ["", "## Additional instructions for this output", ""]
    parts += build_output_directives(language, depth, extras or [])
    parts += [
        "",
        "Follow all rules above. Begin with a `## ` heading and write only this section.",
    ]
    return "\n".join(parts)
