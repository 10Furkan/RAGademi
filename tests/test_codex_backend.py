from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dersnotu.llm.codex_client import (
    CodexSubscriptionClient,
    _prompt_and_images,
    _resolve_executable,
    _usage_from_jsonl,
)
from dersnotu.models import Usage


def _client() -> CodexSubscriptionClient:
    client = CodexSubscriptionClient.__new__(CodexSubscriptionClient)
    client.executable = "codex"
    client.model = ""
    client.cheap_model = ""
    client.effort = "high"
    client.max_tokens = 100
    client.timeout = 10
    client.usage = Usage()
    return client


def test_resolve_executable_uses_configured_path(monkeypatch, tmp_path: Path):
    configured = tmp_path / "codex.exe"
    configured.touch()
    monkeypatch.setenv("RAGADEMI_CODEX_PATH", str(configured))
    monkeypatch.setattr("dersnotu.llm.codex_client.shutil.which", lambda _: None)

    assert _resolve_executable() == str(configured)


def test_resolve_executable_falls_back_to_windows_extension(monkeypatch):
    monkeypatch.delenv("RAGADEMI_CODEX_PATH", raising=False)
    monkeypatch.setattr("dersnotu.llm.codex_client.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "dersnotu.llm.codex_client._windows_extension_codex",
        lambda: r"C:\\extension\\codex.exe",
    )

    assert _resolve_executable() == r"C:\\extension\\codex.exe"


def test_prompt_keeps_system_text_and_extracts_images(tmp_path: Path):
    prompt, images = _prompt_and_images(
        "system rule",
        [
            {"type": "text", "text": "book excerpt"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "iVBORw0KGgo=",
                },
            },
        ],
        tmp_path,
    )
    assert "system rule" in prompt
    assert "book excerpt" in prompt
    assert len(images) == 1 and images[0].read_bytes().startswith(b"\x89PNG")


def test_command_is_ephemeral_read_only_and_uses_schema(tmp_path: Path):
    client = _client()
    output, schema = tmp_path / "out.txt", tmp_path / "schema.json"
    cmd = client._command(tmp_path, output, schema, [])
    assert cmd[:2] == ["codex", "exec"]
    assert "--ephemeral" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert cmd[cmd.index("--output-schema") + 1] == str(schema)
    assert cmd[-1] == "-"


def test_usage_is_read_from_last_json_event():
    stdout = "\n".join(
        [
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3
            }}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 6
            }}),
        ]
    )
    usage = _usage_from_jsonl(stdout)
    assert usage.input_tokens == 20
    assert usage.cache_read_tokens == 8
    assert usage.output_tokens == 6
    assert usage.calls == 1


def test_complete_reads_structured_output_and_reports_delta(monkeypatch):
    client = _client()
    deltas: list[str] = []

    def fake_run(cmd, **kwargs):
        output = Path(cmd[cmd.index("--output-last-message") + 1])
        output.write_text('{"ok": true}', encoding="utf-8")
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("dersnotu.llm.codex_client.subprocess.run", fake_run)
    result = client.complete(
        system="Return JSON.",
        content=[{"type": "text", "text": "Confirm."}],
        json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        on_delta=deltas.append,
    )

    assert result.structured == {"ok": True}
    assert result.usage.input_tokens == 12
    assert deltas == ['{"ok": true}']
