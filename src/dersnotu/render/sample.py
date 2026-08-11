"""Built-in document used to validate the rendering pipeline without an API."""

from __future__ import annotations

from ..models import ExpandedSection, StudyDocument

_ONE = r"""
## Binary Representation of Integers

A computer stores integers as either **unsigned** or **signed** values. The bit
pattern does not change; only its interpretation changes. [S: 22]

### Unsigned interpretation

For a $w$-bit vector $\vec{x} = [x_{w-1}, x_{w-2}, \dots, x_0]$:

$$B2U_w(\vec{x}) = \sum_{i=0}^{w-1} x_i 2^i$$

The range is $0 \le B2U_w(\vec{x}) \le 2^w - 1$. [B: Integer Representations, p. 96-98]

### Two's-complement interpretation

In two's complement, the most significant bit has a **negative** weight:

$$B2T_w(\vec{x}) = -x_{w-1}2^{w-1} + \sum_{i=0}^{w-2} x_i2^i$$

| Representation | Binary | Hexadecimal | Decimal |
|---|---:|---:|---:|
| Unsigned | `11001010` | `0xCA` | 202 |
| Two's complement | `11001010` | `0xCA` | -54 |

[FIGURE: slide 22]

> ⚠️ Common mistake: negating `TMin` overflows and returns `TMin` again.

::: analogy
Think of two's complement as a car odometer wrapping backward from zero.

**Where it breaks down:** only the most significant bit carries negative weight.
:::

::: quiz
1. What decimal value does `0xB4` represent as an 8-bit signed integer?
2. Why do `TMin` and `-TMin` have the same bit pattern?

**Answers:** 1) $-76$ 2) the positive counterpart is outside the representable range.
:::

::: glossary
| Term | Meaning |
|---|---|
| Two's complement | Signed representation with a negative most-significant-bit weight |
| Overflow | A result outside the representable range |
:::
"""

_TWO = r"""
## Signed and Unsigned Conversion

A C cast between signed and unsigned integer types **preserves the bit pattern
and changes its interpretation**. [B: Integer Representations, p. 104-106]

```c
short x = -12345;
unsigned short ux = (unsigned short)x;  /* 53191 */
```

Because $-12345 + 2^{16} = 53191$, conversion adds $2^w$ to the negative value.

### The real trap: implicit conversion

When signed and unsigned operands are mixed, the signed operand is converted to
unsigned. Comparison operators follow the same rule:

```c
if (-1 < 0u) { /* false: -1 becomes 4294967295u */ }
```

The rule matters because `sizeof` returns an unsigned value. [S: 27, 28]
"""

_THREE = r"""
## Bitwise Operations

In C, `&`, `|`, `~`, and `^` operate element-by-element on bit vectors. [S: 18]

```c
unsigned char a = 0x69;
unsigned char b = 0x55;
a & b;   /* intersection */
a | b;   /* union */
```

Do not confuse them with logical `&&`, `||`, and `!`, which always produce 0 or 1.
"""


def sample_document() -> StudyDocument:
    return StudyDocument(
        lecture_title="Computer Systems: Data Representation",
        language="English",
        sections=[
            ExpandedSection(
                section_index=0,
                title="Binary Representation of Integers",
                markdown=_ONE,
                citations=["Integer Representations, p. 96-100"],
                slide_range=(18, 24),
            ),
            ExpandedSection(
                section_index=1,
                title="Signed and Unsigned Conversion",
                markdown=_TWO,
                citations=["Integer Representations, p. 104-106"],
                slide_range=(25, 29),
            ),
            ExpandedSection(
                section_index=2,
                title="Bitwise Operations",
                markdown=_THREE,
                citations=[],
                slide_range=(30, 34),
            ),
            ExpandedSection(
                section_index=3,
                title="Multiplication and Division",
                markdown="",
                citations=[],
                slide_range=(35, 41),
                error="RateLimitError (sample)",
            ),
        ],
    )
