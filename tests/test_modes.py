"""Derinlik / ek anlatım / dil seçenekleri.

İki ayrı sözleşme sınanıyor:
  * Seçimler prompta GERÇEKTEN geçiyor mu (sessizce yutulmuyor mu)?
  * Seçimler cache önekini kirletmiyor mu — direktifler kırılma noktasından
    SONRA gelmeli, yoksa iki farklı derinlik cache paylaşamaz.
"""

from __future__ import annotations

import pytest

from dersnotu.llm.prompts import (
    DEPTHS,
    EXPAND_SYSTEM,
    EXTRAS,
    build_lecture_context,
    build_output_directives,
    build_section_request,
)
from dersnotu.models import BookChunk, TopicCard
from dersnotu.pipeline import build_cached_prefix
from tests.test_pipeline import make_lecture

CHUNK = BookChunk(
    chunk_id="c1",
    text="içerik",
    section_title="Integer Representations",
    page_start=96,
    page_end=98,
    token_estimate=5,
)


def request_for(**kw) -> str:
    lec = make_lecture()
    card = TopicCard(section_index=0, title="Konu")
    kw.setdefault("language", "English")
    return build_section_request(lec.sections[0], card, [CHUNK], kw.pop("language"), **kw)


# --- Derinlik ---------------------------------------------------------------
@pytest.mark.parametrize("depth", list(DEPTHS))
def test_every_depth_is_accepted(depth):
    assert request_for(depth=depth)


def test_depths_produce_different_prompts():
    """Aksi halde arayüzdeki seçim hiçbir şey yapmıyor demektir."""
    summary, deep = request_for(depth="summary"), request_for(depth="deep")
    assert summary != deep
    assert "DEPTH: SUMMARY" in summary and "DEPTH: SUMMARY" not in deep
    assert "DEPTH: DEEP" in deep


def test_standard_adds_no_depth_directive():
    """Varsayılan davranış sistem promptunda; tekrar etmek onu zayıflatır."""
    assert "DEPTH:" not in request_for(depth="standard")


# --- Ek anlatım -------------------------------------------------------------
@pytest.mark.parametrize("extra", list(EXTRAS))
def test_each_extra_reaches_the_prompt(extra):
    assert EXTRAS[extra][:40] in request_for(extras=[extra])


def test_unselected_extras_are_absent():
    text = request_for(extras=["quiz"])
    assert "::: quiz" in text
    assert "::: analogy" not in text
    assert "::: glossary" not in text


def test_unknown_extra_is_ignored_not_crashed():
    assert request_for(extras=["yokböyle"])


def test_extras_combine():
    text = request_for(extras=["analogy", "quiz", "glossary"])
    for marker in ("::: analogy", "::: quiz", "::: glossary"):
        assert marker in text


# --- Dil --------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["English", "Turkish", "Deutsch"])
def test_language_reaches_the_prompt(lang):
    assert f"Write this section in {lang}" in request_for(language=lang)


def test_citation_markers_are_protected_from_translation():
    """`[K: ...]` regex ile ayrıştırılıyor; çevrilirse atıf zinciri kopar."""
    text = request_for(language="English")
    assert "preserve" in text
    assert "[B: ...]" in text


# --- Cache bütünlüğü --------------------------------------------------------
def test_every_option_has_a_user_facing_explanation():
    """Açıklamalar direktiflerin yanında duruyor ve arayüz onları /api/health'ten
    okuyor. Yeni bir seçenek eklenip açıklaması unutulursa kullanıcı ne
    seçtiğini bilmeden seçer."""
    from dersnotu.llm.prompts import DEPTH_HELP, EXTRA_HELP

    assert set(DEPTH_HELP) == set(DEPTHS)
    assert set(EXTRA_HELP) == set(EXTRAS)
    for metin in (*DEPTH_HELP.values(), *EXTRA_HELP.values()):
        assert len(metin) > 40, "help text should explain the option"


def test_analogy_help_states_the_limit_rule():
    """Analojinin ayırt edici kuralı 'nerede bozulur'; açıklama bunu atlarsa
    kullanıcı seçeneği sıradan bir benzetme sanır."""
    from dersnotu.llm.prompts import EXTRA_HELP

    assert "breaks down" in EXTRA_HELP["analogy"]
    assert "fails" in EXTRAS["analogy"]


def test_backend_status_explains_each_option():
    from dersnotu.llm import backend_status

    b = backend_status()
    for key in ("api", "cli", "codex", "demo"):
        assert len(b[key]["help"]) > 40
    # Abonelik yolunun iki gerçeği kullanıcıya söylenmeli: ücretsiz ama yavaş.
    assert "quota" in b["cli"]["help"]
    assert "slower" in b["cli"]["help"]


def test_directives_do_not_touch_the_system_prompt():
    """Sistem promptu cache önekinin ilk baytı — sabit kalmalı."""
    for depth in DEPTHS:
        assert depth not in EXPAND_SYSTEM
    for extra in EXTRAS:
        assert f"::: {extra}" not in EXPAND_SYSTEM


def test_cached_prefix_is_identical_across_depths_and_languages():
    """Önek dilden/derinlikten bağımsız olmalı; yoksa her seçim cache'i düşürür."""
    lec = make_lecture()
    assert build_cached_prefix(lec) == build_cached_prefix(lec)
    assert build_lecture_context(lec) == build_lecture_context(lec)
    # Direktifler önekte DEĞİL, gövdede.
    prefix_text = build_cached_prefix(lec)[0]["text"]
    for line in build_output_directives("English", "deep", ["quiz"]):
        assert line not in prefix_text
