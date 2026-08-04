"""Claude Code CLI üzerinden çalışan istemci — Claude Pro/Max aboneliği için.

Neden var: Messages API kredili bir API anahtarı ister. Claude Pro/Max
aboneliği ise `claude` komutunun oturumuyla doğrulanır. Bu istemci `claude -p`
alt sürecini sürerek aynı boru hattını abonelikle çalıştırır; `LLMClient` ile
BİREBİR aynı yüzeyi sunar, `pipeline` hangisini aldığını bilmez.

Dört davranış ölçülerek bulundu; hiçbiri belgede yazmıyor ve üçü sessizce
pahalıya veya hataya mal oluyor:

1. VARSAYILAN ÇAĞRI AJAN KOŞUMUNU YÜKLER. Düz `claude -p "ok"` çağrısı 2
   token'lık bir istem için 40.915 token girdi harcıyor: Claude Code'un kendi
   sistem promptu, araç tanımları, CLAUDE.md ve ayarlar. `_SLIM_FLAGS` bunu
   196 token'a indiriyor (ölçüldü). 8 bölümlük bir derste fark ~325K token.

2. `cache_control` GÖNDERİLEMEZ. CLI kendi 1 saatlik TTL'li cache bloğunu
   ekliyor; bizim 5 dakikalık bloğumuz sıralama kuralını bozuyor ve API
   `400 ... cache_control.ttl` döndürüyor. Bloklar gönderilmeden önce
   sıyrılır — önbelleği Claude Code yönetir.

3. `--input-format stream-json` ZORUNLU OLARAK `--output-format stream-json`
   ve `--verbose` ister. Görüntüleri göndermenin tek yolu bu: base64 image
   blokları stdin'den NDJSON olarak akıtılır.

4. ABONELİK KOTASI `rate_limit_event` İLE BİLDİRİLİR (`five_hour` penceresi,
   `resetsAt`). API'de böyle bir olay yok; buradan okunup arayüze taşınır.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from ..models import Usage
from .client import CallResult, RefusalError

# Ajan koşumunu kapatan bayraklar. Ölçüm: 40.915 → 196 token.
_SLIM_FLAGS = [
    "--tools", "",                 # yerleşik araçlar (Bash/Read/Edit...) yüklenmesin
    "--setting-sources", "",       # kullanıcı/proje ayarları ve CLAUDE.md okunmasın
    "--strict-mcp-config",         # MCP sunucuları yüklenmesin
    "--disable-slash-commands",    # skill'ler yüklenmesin
    "--no-session-persistence",    # oturum diske yazılmasın
]


class ClaudeCodeUnavailable(RuntimeError):
    pass


def _strip_cache_control(content: list[dict]) -> list[dict]:
    """`cache_control` alanlarını çıkarır (yukarıdaki 2. madde)."""
    return [{k: v for k, v in block.items() if k != "cache_control"} for block in content]


# Süreç genelinde en son görülen kota penceresi.
#
# Kota istemci örneğine değil ABONELİĞE ait: her iş kendi istemcisini kurup
# attığı için `self.rate_limit` işle birlikte kaybolur ve arayüz "kotam ne
# durumda" sorusunu asla yanıtlayamaz. Sorgulanabilir bir uç da yok — bilgi
# yalnızca bir çağrının akışında geliyor, o yüzden geçerken yakalanıyor.
_last_rate_limit: dict[str, Any] | None = None


def last_rate_limit() -> dict[str, Any] | None:
    """En son çağrıda bildirilen kota durumu; hiç çağrı olmadıysa None."""
    return _last_rate_limit


class ClaudeCodeClient:
    """`LLMClient` ile aynı yüzey; arkada `claude -p` alt süreci."""

    def __init__(
        self,
        model: str,
        *,
        cheap_model: str,
        effort: str = "high",
        max_tokens: int = 16000,
        executable: str = "claude",
        timeout: int = 900,
    ):
        self.model = model
        self.cheap_model = cheap_model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.usage = Usage()
        self.rate_limit: dict[str, Any] | None = None

        exe = shutil.which(executable)
        if exe is None:
            raise ClaudeCodeUnavailable(
                "`claude` komutu bulunamadı. Claude Code kurulu mu? "
                "Kurulum: https://claude.com/claude-code"
            )
        self.executable = exe

    # ------------------------------------------------------------------
    @staticmethod
    def available(executable: str = "claude") -> bool:
        return shutil.which(executable) is not None

    @classmethod
    def auth_mode(cls, executable: str = "claude") -> str | None:
        """Hangi kimlikle çalışıyor: 'subscription' | 'api_key' | None.

        `system/init` olayındaki `apiKeySource` alanından okunur:
        "none" → abonelik oturumu, aksi halde bir anahtar kaynağı.
        """
        if not cls.available(executable):
            return None
        try:
            proc = subprocess.run(
                [shutil.which(executable), "-p", "ok",
                 "--output-format", "stream-json", "--verbose",
                 "--system-prompt", "x", *_SLIM_FLAGS],
                capture_output=True, text=True, encoding="utf-8-sig", timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                src = ev.get("apiKeySource")
                return "subscription" if src in (None, "none") else "api_key"
        return None

    # ------------------------------------------------------------------
    def _command(self, model: str, json_schema: dict | None) -> list[str]:
        cmd = [
            self.executable, "-p",
            "--input-format", "stream-json",
            # stream-json girdi, stream-json çıktı + --verbose ZORUNLU kılıyor.
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", model,
            *_SLIM_FLAGS,
        ]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]
        return cmd

    def _record(self, raw: dict) -> Usage:
        u = Usage(
            input_tokens=raw.get("input_tokens", 0) or 0,
            output_tokens=raw.get("output_tokens", 0) or 0,
            cache_creation_tokens=raw.get("cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=raw.get("cache_read_input_tokens", 0) or 0,
            calls=1,
        )
        self.usage.add(u)
        return u

    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        system: str | list[dict],
        content: list[dict],
        model: str | None = None,
        json_schema: dict | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallResult:
        model = model or self.model

        # Sistem promptu liste olarak gelebilir (cache blokları); CLI düz metin ister.
        if isinstance(system, list):
            system_text = "\n\n".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
        else:
            system_text = system

        cmd = self._command(model, json_schema)
        cmd += ["--system-prompt", system_text]

        payload = {
            "type": "user",
            "message": {"role": "user", "content": _strip_cache_control(content)},
        }
        stdin = json.dumps(payload, ensure_ascii=False) + "\n"

        env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_tokens)}
        try:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True,
                encoding="utf-8-sig", timeout=self.timeout, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI {self.timeout}s içinde yanıt vermedi") from exc

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI hata verdi (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '')[:600]}"
            )

        return self._parse(proc.stdout, on_delta)

    # ------------------------------------------------------------------
    def _parse(self, stdout: str, on_delta: Callable[[str], None] | None) -> CallResult:
        chunks: list[str] = []
        result_ev: dict | None = None

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = ev.get("type")
            if kind == "stream_event":
                inner = ev.get("event", {})
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta", {})
                    # thinking_delta / signature_delta atlanır, yalnızca metin.
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            chunks.append(text)
                            if on_delta:
                                on_delta(text)
            elif kind == "rate_limit_event":
                global _last_rate_limit
                self.rate_limit = _last_rate_limit = ev.get("rate_limit_info")
            elif kind == "result":
                result_ev = ev

        if result_ev is None:
            raise RuntimeError("claude CLI `result` olayı döndürmedi")

        if result_ev.get("is_error"):
            raise RuntimeError(f"claude CLI: {str(result_ev.get('result'))[:400]}")

        usage = self._record(result_ev.get("usage") or {})
        stop = result_ev.get("stop_reason")
        if stop == "refusal":
            raise RefusalError(None, str(result_ev.get("result"))[:300])

        # Akış deltaları boş kalırsa `result` metnine düş. `or` yetmiyor:
        # --json-schema modunda çıktı `StructuredOutput` aracına gidiyor ve
        # metin kanalından yalnızca boşluk akıyor. " " truthy olduğu için
        # ham `or` bunu geçerli sanıp JSON ayrıştırmasını patlatıyordu.
        text = "".join(chunks)
        if not text.strip():
            text = result_ev.get("result") or ""
        return CallResult(
            text=text,
            usage=usage,
            stop_reason=stop,
            structured=result_ev.get("structured_output"),
        )

    # ------------------------------------------------------------------
    def complete_json(
        self, *, system: str, content: list[dict], schema: dict, model: str | None = None
    ) -> Any:
        result = self.complete(
            system=system, content=content, model=model or self.cheap_model, json_schema=schema
        )
        # Kanonik kaynak: CLI şemayı `StructuredOutput` aracıyla uyguluyor ve
        # ayrıştırılmış nesneyi burada veriyor. Metin kanalı boş olabiliyor.
        if isinstance(result.structured, (dict, list)):
            return result.structured

        text = (result.text or "").strip()
        if not text:
            raise RuntimeError(
                "claude CLI yapılandırılmış çıktı döndürmedi "
                f"(stop_reason={result.stop_reason}). Şema çok mu karmaşık?"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise
