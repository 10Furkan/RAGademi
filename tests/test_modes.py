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
    kw.setdefault("language", "Türkçe")
    return build_section_request(lec.sections[0], card, [CHUNK], kw.pop("language"), **kw)


# --- Derinlik ---------------------------------------------------------------
@pytest.mark.parametrize("depth", list(DEPTHS))
def test_every_depth_is_accepted(depth):
    assert request_for(depth=depth)


def test_depths_produce_different_prompts():
    """Aksi halde arayüzdeki seçim hiçbir şey yapmıyor demektir."""
    özet, derin = request_for(depth="özet"), request_for(depth="derin")
    assert özet != derin
    assert "ÖZET" in özet and "ÖZET" not in derin
    assert "DERİN" in derin


def test_standart_adds_no_depth_directive():
    """Varsayılan davranış sistem promptunda; tekrar etmek onu zayıflatır."""
    assert "DERİNLİK:" not in request_for(depth="standart")


# --- Ek anlatım -------------------------------------------------------------
@pytest.mark.parametrize("extra", list(EXTRAS))
def test_each_extra_reaches_the_prompt(extra):
    assert EXTRAS[extra][:40] in request_for(extras=[extra])


def test_unselected_extras_are_absent():
    text = request_for(extras=["soru"])
    assert "::: soru" in text
    assert "::: analoji" not in text
    assert "::: sözlük" not in text


def test_unknown_extra_is_ignored_not_crashed():
    assert request_for(extras=["yokböyle"])


def test_extras_combine():
    text = request_for(extras=["analoji", "soru", "sözlük"])
    for marker in ("::: analoji", "::: soru", "::: sözlük"):
        assert marker in text


# --- Dil --------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["Türkçe", "English", "Deutsch"])
def test_language_reaches_the_prompt(lang):
    assert f"Bu bölümü {lang} dilinde yaz" in request_for(language=lang)


def test_citation_markers_are_protected_from_translation():
    """`[K: ...]` regex ile ayrıştırılıyor; çevrilirse atıf zinciri kopar."""
    text = request_for(language="English")
    assert "harfi harfine" in text
    assert "[K: ...]" in text


# --- Cache bütünlüğü --------------------------------------------------------
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
    for line in build_output_directives("English", "derin", ["soru"]):
        assert line not in prefix_text
