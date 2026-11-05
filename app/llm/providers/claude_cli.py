"""Local Claude CLI adapter.

Uses the same ProviderAdapter contract as API-backed providers, but invokes
the Claude CLI in one-shot mode via subprocess instead of making an HTTP call.
"""
import asyncio
import os
import shlex
import time
from typing import Sequence

from app.llm.failures import ProviderCallError
from app.llm.providers.base import ProviderRequest, ProviderResponse


class ClaudeCliAdapter:
    provider = "claude_cli"

    def __init__(
        self,
        *,
        command: str,
        runtime_home: str = "",
        prompt_flag: str = "-p",
        model_flag: str = "--model",
    ) -> None:
        self._command = command
        self._runtime_home = runtime_home
        self._prompt_flag = prompt_flag
        self._model_flag = model_flag

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        argv = self._build_command(request)
        env = self._build_env()
        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderCallError(
                "policy_blocked",
                f"Claude CLI binary not found for command {self._command!r}",
                original=exc,
            ) from exc
        except Exception as exc:
            raise ProviderCallError("provider_error", str(exc), original=exc) from exc

        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            await _terminate_process(proc)
            raise
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = stdout.decode("utf-8", errors="replace").strip()
        error_text = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            reason = "provider_error"
            if "login" in error_text.lower() or "auth" in error_text.lower():
                reason = "provider_error"
            raise ProviderCallError(
                reason,
                error_text or f"Claude CLI exited with code {proc.returncode}",
            )
        if not text:
            raise ProviderCallError("empty_output", "Claude CLI returned empty output")

        return ProviderResponse(
            provider=self.provider,
            model=request.model,
            text=text,
            input_tokens=max(1, _estimate_tokens(request.system, request.prompt)),
            output_tokens=max(1, _estimate_tokens(text)),
            latency_ms=latency_ms,
            raw_response={"argv": _sanitize_argv(argv), "stderr": error_text},
        )

    def _build_command(self, request: ProviderRequest) -> list[str]:
        command = shlex.split(self._command)
        command.extend([self._prompt_flag, _prompt_text(request)])
        if request.model:
            command.extend([self._model_flag, request.model])
        return command

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._runtime_home:
            env["HOME"] = self._runtime_home
        return env


def _prompt_text(request: ProviderRequest) -> str:
    if request.system and request.system.strip():
        return f"{request.system.strip()}\n\n{request.prompt}"
    return request.prompt


def _estimate_tokens(*parts: str | None) -> int:
    text = "\n".join(part or "" for part in parts)
    return max(1, len(text) // 4)


def _sanitize_argv(argv: Sequence[str]) -> list[str]:
    return [str(part) for part in argv]


async def _terminate_process(proc) -> None:
    if proc.returncode is not None:
        return
    proc.kill()
    try:
        await proc.wait()
    except Exception:
        return
