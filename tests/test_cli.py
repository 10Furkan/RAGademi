"""Komut satırının dayanıklılığı.

Buradaki tek konu şu: **çıktı yazdırmak komutu öldürmemeli.** Türkçe
Windows'ta konsol kod sayfası cp1254 olabiliyor ve `→` oku ile rich'in tablo
çizgileri (─ │ ┌) o kod sayfasında YOK. Varsayılan `errors="strict"` ile bu
`UnicodeEncodeError` demek — ve hata mesajı değil, komutun tamamı ölüyor:
`serve` başlangıç satırını basamadığı için sunucu hiç ayağa kalkmıyordu.
"""

from __future__ import annotations

import io

from rich.console import Console

from dersnotu.cli import _konsolu_dayanikli_yap, app


def cp1254_akis() -> io.TextIOWrapper:
    """Türkçe Windows konsolunun taklidi."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1254", newline="")


def test_an_unencodable_character_kills_the_stream_by_default():
    """Önce kusuru göster: düzeltme olmadan yazmak PATLIYOR."""
    akis = cp1254_akis()
    try:
        akis.write("dersnotu → http://127.0.0.1:8000")
        akis.flush()
    except UnicodeEncodeError:
        return
    raise AssertionError("cp1254 '→' okunu kabul etmemeliydi; test artık bir şey ölçmüyor")


def test_relaxing_the_error_mode_keeps_the_command_alive():
    akis = cp1254_akis()
    akis.reconfigure(errors="replace")
    akis.write("dersnotu → http://127.0.0.1:8000")
    akis.flush()
    yazilan = akis.buffer.getvalue().decode("cp1254")
    assert "http://127.0.0.1:8000" in yazilan  # asıl bilgi duruyor
    assert "?" in yazilan  # ok yerine yer tutucu


def test_rich_tables_survive_a_non_utf8_stream():
    """`inspect` tabloyu çizerken ölüyordu: kutu çizgileri cp1254'te yok."""
    from rich.table import Table

    akis = cp1254_akis()
    akis.reconfigure(errors="replace")
    t = Table(title="Bölüm 0 — slayt 1-7")
    t.add_column("Başlık")
    t.add_column("Görsel")
    t.add_row("Encoding Byte Values", "✔")
    Console(file=akis, width=60).print(t)  # patlamamalı
    assert "Encoding Byte Values" in akis.buffer.getvalue().decode("cp1254")


def test_hardening_a_stream_without_reconfigure_is_a_noop(monkeypatch):
    """Yeniden yönlendirilmiş akış (pytest, boru) `reconfigure` taşımayabilir;
    düzeltme orada sessizce geçmeli, yoksa CLI import edilemez olur."""
    import sys

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    _konsolu_dayanikli_yap()  # patlamamalı


def test_practice_command_is_registered():
    adlar = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert "practice" in adlar
