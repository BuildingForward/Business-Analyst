"""LLM access for the analyst.

Providers are pluggable so the pipeline can run three ways:
  * `anthropic` - the real thing, via the official SDK;
  * `dry-run`   - renders prompts and returns a placeholder, no network, no spend;
  * `echo`      - returns the prompt itself, used by the tests.

A continuous bot spends real money, so responses are cached on disk by
prompt hash and the caller is given a hard per-run call budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 64_000


class BudgetExhausted(RuntimeError):
    """Raised when a run has used its allotted number of model calls."""


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    stop_reason: str = ""


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> LLMResponse: ...


@dataclass
class DryRunProvider:
    """Renders prompts without calling anything. Used to inspect what would be sent."""

    name: str = "dry-run"
    model: str = DEFAULT_MODEL

    def complete(self, system: str, prompt: str) -> LLMResponse:
        preview = prompt.strip()
        return LLMResponse(
            text=(
                "[dry run - no model was called]\n\n"
                f"System prompt: {len(system)} chars\n"
                f"User prompt: {len(prompt)} chars\n\n"
                "--- prompt that would be sent ---\n"
                f"{preview}"
            ),
            model=f"{self.model} (dry-run)",
            tokens_in=0,
            tokens_out=0,
        )


@dataclass
class EchoProvider:
    """Deterministic stand-in so tests can assert on pipeline behaviour."""

    name: str = "echo"
    model: str = "echo"
    prefix: str = "ECHO"

    def complete(self, system: str, prompt: str) -> LLMResponse:
        return LLMResponse(
            text=f"{self.prefix}: {prompt[:400]}",
            model=self.model,
            tokens_in=len(prompt) // 4,
            tokens_out=100,
        )


@dataclass
class AnthropicProvider:
    """Calls Claude through the official SDK.

    Streaming is used throughout: the analyst prompts ask for long, structured
    answers, and a non-streaming request at this `max_tokens` risks an HTTP
    timeout.
    """

    name: str = "anthropic"
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str = "high"
    api_key: Optional[str] = None
    # On a policy decline the API re-runs the request on a fallback model
    # inside the same call, rather than the analysis simply stopping.
    refusal_fallback: bool = True
    _client: object = field(default=None, repr=False, compare=False)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The 'anthropic' package is required for the anthropic provider. "
                "Install it with `pip install anthropic`, or run with "
                "--provider dry-run to render prompts without calling the API."
            ) from exc
        self._client = (
            anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
        )
        return self._client

    def complete(self, system: str, prompt: str) -> LLMResponse:
        import anthropic

        client = self._get_client()
        # The system prompt is identical across every playbook and every deal,
        # so it is marked cacheable. Note the API only caches prefixes above a
        # model-dependent minimum, so this pays off on long briefs, not short ones.
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            message = self._stream(client, kwargs, use_fallback=self.refusal_fallback)
        except anthropic.BadRequestError as exc:
            # An account or proxy that does not know the fallback beta should not
            # take the whole run down; retry once on the stable endpoint.
            if self.refusal_fallback and "fallback" in str(exc).lower():
                log.warning("Refusal fallback rejected (%s); retrying without it.", exc)
                self.refusal_fallback = False
                message = self._stream(client, kwargs, use_fallback=False)
            else:
                raise

        stop_reason = getattr(message, "stop_reason", "") or ""
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return LLMResponse(
                text=(
                    "[the model declined this request"
                    + (f" (category: {category})" if category else "")
                    + "]"
                ),
                model=self.model,
                stop_reason=stop_reason,
            )

        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text=text.strip(),
            model=self.model,
            tokens_in=getattr(usage, "input_tokens", 0) or 0,
            tokens_out=getattr(usage, "output_tokens", 0) or 0,
            stop_reason=stop_reason,
        )

    def _stream(self, client, kwargs: dict, use_fallback: bool):
        if use_fallback:
            with client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            ) as stream:
                return stream.get_final_message()
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()


class ResponseCache:
    """Disk cache keyed by (model, system, prompt) so reruns cost nothing."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(model: str, system: str, prompt: str) -> str:
        digest = hashlib.sha256(f"{model}\x00{system}\x00{prompt}".encode("utf-8"))
        return digest.hexdigest()[:32]

    def get(self, key: str) -> Optional[LLMResponse]:
        f = self.path / f"{key}.json"
        if not f.exists():
            return None
        try:
            payload = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return LLMResponse(
            text=payload["text"],
            model=payload.get("model", ""),
            tokens_in=payload.get("tokens_in", 0),
            tokens_out=payload.get("tokens_out", 0),
            cached=True,
            stop_reason=payload.get("stop_reason", ""),
        )

    def put(self, key: str, response: LLMResponse) -> None:
        f = self.path / f"{key}.json"
        f.write_text(
            json.dumps(
                {
                    "text": response.text,
                    "model": response.model,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "stop_reason": response.stop_reason,
                    "cached_at": time.time(),
                },
                indent=2,
            )
        )


@dataclass
class Client:
    """Provider plus cache plus a spend guard."""

    provider: Provider
    cache: Optional[ResponseCache] = None
    max_calls: Optional[int] = None
    calls_made: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 2
    retry_backoff: float = 2.0

    def complete(self, system: str, prompt: str) -> LLMResponse:
        model = getattr(self.provider, "model", self.provider.name)
        if self.cache:
            key = self.cache.key(model, system, prompt)
            hit = self.cache.get(key)
            if hit:
                log.debug("cache hit %s", key)
                return hit

        if self.max_calls is not None and self.calls_made >= self.max_calls:
            raise BudgetExhausted(
                f"Call budget of {self.max_calls} model calls is spent for this run."
            )

        response = self._complete_with_retry(system, prompt)
        self.calls_made += 1
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out

        if self.cache and response.text:
            self.cache.put(self.cache.key(model, system, prompt), response)
        return response

    def _complete_with_retry(self, system: str, prompt: str) -> LLMResponse:
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self.provider.complete(system, prompt)
            except Exception as exc:  # noqa: BLE001 - provider-specific below
                if not _is_retryable(exc) or attempt == self.retries:
                    raise
                last = exc
                delay = self.retry_backoff ** (attempt + 1)
                log.warning("Model call failed (%s); retrying in %.0fs", exc, delay)
                time.sleep(delay)
        raise last  # pragma: no cover - loop always returns or raises


def _is_retryable(exc: Exception) -> bool:
    """Only transient failures are worth a second call; a 400 never is.

    The SDK already retries 429/5xx internally, so this is the outer net for
    long streaming calls that die mid-flight.
    """
    try:
        import anthropic
    except ImportError:
        return False
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def build_provider(name: str, model: str = DEFAULT_MODEL, effort: str = "high") -> Provider:
    name = (name or "").lower()
    if name in ("anthropic", "claude", "api"):
        return AnthropicProvider(model=model, effort=effort)
    if name in ("dry-run", "dryrun", "dry"):
        return DryRunProvider(model=model)
    if name == "echo":
        return EchoProvider()
    raise ValueError(f"Unknown provider '{name}'. Use anthropic, dry-run or echo.")


def default_client(
    provider: str = "dry-run",
    model: str = DEFAULT_MODEL,
    cache_dir: Optional[Path] = None,
    max_calls: Optional[int] = None,
    effort: str = "high",
) -> Client:
    if provider in ("anthropic", "claude", "api") and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        log.warning(
            "No ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set; the SDK will look for "
            "an `ant auth login` profile."
        )
    return Client(
        provider=build_provider(provider, model, effort),
        cache=ResponseCache(cache_dir) if cache_dir else None,
        max_calls=max_calls,
    )
