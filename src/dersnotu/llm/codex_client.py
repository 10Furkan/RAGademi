"""Codex CLI client for using a ChatGPT/Codex subscription.

The public interface matches `LLMClient` and `ClaudeCodeClient` so the RAG
pipeline remains provider-agnostic. Calls run non-interactively in an empty,
read-only temporary workspace. The CLI receives only the prompt and explicitly
attached images; it cannot modify the project.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import Usage
from .client import CallResult, RefusalError


class CodexUnavailable(RuntimeError):
    pass


def _windows_extension_codex() -> str | None:
    """Find the Codex binary bundled with a local editor extension.

    VS Code launches integrated terminals with the extension's Codex directory
    on PATH, but a separately started web server may inherit an older PATH.
    Looking in the extension install directories keeps the subscription backend
    available in that common Windows setup.
    """
    if os.name != "nt":
        return None

    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        return None

    roots = (
        Path(user_profile) / ".vscode" / "extensions",
        Path(user_profile) / ".vscode-insiders" / "extensions",
    )
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            root.glob("openai.chatgpt-*/bin/windows-*/codex.exe")
        )
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    newest = max(existing, key=lambda path: path.stat().st_mtime_ns)
    return str(newest)


def _resolve_executable(executable: str = "codex") -> str | None:
    """Resolve Codex from configuration, PATH, or a Windows editor bundle."""
    if executable == "codex":
        configured = os.environ.get("RAGADEMI_CODEX_PATH", "").strip()
        if configured:
            found = shutil.which(configured)
            if found:
                return found
            configured_path = Path(configured).expanduser()
            if configured_path.is_file():
                return str(configured_path)

    found = shutil.which(executable)
    if found:
        return found
    if executable == "codex":
        return _windows_extension_codex()
    return None


def _system_text(system: str | list[dict]) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(
        block.get("text", "")
        for block in system
        if block.get("type") == "text" and block.get("text")
    )


def _prompt_and_images(
    system: str | list[dict], content: list[dict], directory: Path
) -> tuple[str, list[Path]]:
    text_blocks: list[str] = []
    images: list[Path] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            text_blocks.append(block.get("text", ""))
        elif kind == "image":
            source = block.get("source") or {}
            if source.get("type") != "base64":
                continue
            media = source.get("media_type", "image/png")
            suffix = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                media, ".png"
            )
            path = directory / f"image-{len(images) + 1}{suffix}"
            path.write_bytes(base64.b64decode(source.get("data", "")))
            images.append(path)

    prompt = (
        "Follow the system instructions below. Do not use tools or inspect the "
        "workspace; answer only from the supplied content.\n\n"
        "<system>\n"
        + _system_text(system)
        + "\n</system>\n\n<user>\n"
        + "\n\n".join(text_blocks)
        + "\n</user>"
    )
    return prompt, images


def _usage_from_jsonl(stdout: str) -> Usage:
    """Read the last token-usage object from Codex JSONL events."""
    found: dict[str, Any] | None = None

    def visit(value: Any) -> None:
        nonlocal found
        if isinstance(value, dict):
            if "input_tokens" in value and "output_tokens" in value:
                found = value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in stdout.splitlines():
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue

    raw = found or {}
    return Usage(
        input_tokens=raw.get("input_tokens", 0) or 0,
        output_tokens=raw.get("output_tokens", 0) or 0,
        cache_read_tokens=(
            raw.get("cached_input_tokens", raw.get("cache_read_input_tokens", 0)) or 0
        ),
        calls=1,
    )


class CodexSubscriptionClient:
    """`LLMClient`-compatible adapter around `codex exec`."""

    def __init__(
        self,
        model: str = "",
        *,
        cheap_model: str = "",
        effort: str = "high",
        max_tokens: int = 16000,
        executable: str = "codex",
        timeout: int = 900,
    ):
        exe = _resolve_executable(executable)
        if exe is None:
            raise CodexUnavailable(
                "`codex` command not found. Install Codex CLI, run `codex login`, "
                "and sign in with the ChatGPT account that has Codex access."
            )
        self.executable = exe
        self.model = model
        self.cheap_model = cheap_model or model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.usage = Usage()

    @classmethod
    def auth_mode(cls, executable: str = "codex") -> str | None:
        exe = _resolve_executable(executable)
        if exe is None:
            return None
        try:
            proc = subprocess.run(
                [exe, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8-sig",
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = f"{proc.stdout}\n{proc.stderr}".lower()
        if proc.returncode != 0 or "not logged in" in text:
            return None
        if "api key" in text:
            return "api_key"
        return "subscription"

    @classmethod
    def available(cls, executable: str = "codex") -> bool:
        return cls.auth_mode(executable) == "subscription"

    def _command(
        self,
        directory: Path,
        output: Path,
        schema_path: Path | None,
        images: list[Path],
    ) -> list[str]:
        cmd = [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--cd",
            str(directory),
            "--output-last-message",
            str(output),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if schema_path is not None:
            cmd += ["--output-schema", str(schema_path)]
        for image in images:
            cmd += ["--image", str(image)]
        cmd.append("-")
        return cmd

    def complete(
        self,
        *,
        system: str | list[dict],
        content: list[dict],
        model: str | None = None,
        json_schema: dict | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallResult:
        # `model` can contain an Anthropic model passed by shared pipeline code.
        # Codex always uses its own configured model (or the CLI default).
        with tempfile.TemporaryDirectory(prefix="ragademi-codex-") as tmp:
            directory = Path(tmp)
            output = directory / "last-message.txt"
            schema_path: Path | None = None
            if json_schema is not None:
                schema_path = directory / "schema.json"
                schema_path.write_text(
                    json.dumps(json_schema, ensure_ascii=False), encoding="utf-8"
                )

            prompt, images = _prompt_and_images(system, content, directory)
            cmd = self._command(directory, output, schema_path, images)
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8-sig",
                    timeout=self.timeout,
                    env=None,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Codex CLI did not respond within {self.timeout}s"
                ) from exc

            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "")[:800]
                raise RuntimeError(
                    "Codex CLI failed. Run `codex login status` and make sure the "
                    f"ChatGPT subscription is active (rc={proc.returncode}): {detail}"
                )

            text = (
                output.read_text(encoding="utf-8-sig").strip()
                if output.exists()
                else ""
            )
            if not text:
                raise RuntimeError("Codex CLI returned no final message")

            usage = _usage_from_jsonl(proc.stdout)
            self.usage.add(usage)
            if on_delta:
                on_delta(text)

            if text.lower().startswith("refusal:"):
                raise RefusalError(None, text[:300])

            structured: Any = None
            if json_schema is not None:
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    structured = None
            return CallResult(
                text=text,
                usage=usage,
                stop_reason="end_turn",
                structured=structured,
            )

    def complete_json(
        self,
        *,
        system: str,
        content: list[dict],
        schema: dict,
        model: str | None = None,
    ) -> Any:
        result = self.complete(
            system=system,
            content=content,
            json_schema=schema,
        )
        if isinstance(result.structured, (dict, list)):
            return result.structured
        try:
            return json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex CLI did not return valid structured JSON") from exc
