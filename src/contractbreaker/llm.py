"""Optional OpenAI-backed extraction fallback transport.

The SDK is imported lazily so deterministic ContractBreaker analysis and the
offline test suite work without the optional dependency or an API key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_LLM_MODEL = "gpt-4.1-mini"
DEFAULT_LLM_TIMEOUT_SECONDS = 20.0
MAX_LLM_TOKENS = 2000

ALLOWED_FACT_TYPES = (
    "method",
    "endpoint",
    "timeout",
    "retry_count",
    "max_attempts",
    "retry_triggers",
    "idempotency_key_present",
    "idempotency_key_reused",
    "retryable_statuses",
)

_SYSTEM_PROMPT = (
    "You extract only explicit reliability facts from the supplied Python "
    "source excerpt. Return only facts supported by exact source evidence. "
    "Never invent retry limits, timeout values, defaults, library behavior, or "
    "functions whose implementation is absent. Never return verdicts, scenario "
    "configuration, or recommendations. Keep reasons concise and evidence-based; "
    "do not provide chain-of-thought. If evidence is insufficient, use an "
    "unknown, ambiguous, or unsupported status with a null value."
)

_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "target_id",
                    "fact_type",
                    "value",
                    "status",
                    "evidence",
                    "reason",
                    "limitations",
                ],
                "properties": {
                    "target_id": {"type": "string"},
                    "fact_type": {
                        "type": "string",
                        "enum": list(ALLOWED_FACT_TYPES),
                    },
                    "value": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {
                                "type": "array",
                                "items": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "number"},
                                    ]
                                },
                            },
                            {"type": "null"},
                        ]
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "known",
                            "absent",
                            "unknown",
                            "ambiguous",
                            "unsupported",
                        ],
                    },
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "start_line",
                                "end_line",
                                "excerpt",
                            ],
                            "properties": {
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                                "excerpt": {"type": "string"},
                            },
                        },
                    },
                    "reason": {"type": "string"},
                    "limitations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}


class LLMError(RuntimeError):
    """Base error for optional LLM fallback operations."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class LLMUnavailable(LLMError):
    """The fallback could not call the OpenAI API."""


class LLMInvalidOutput(LLMError):
    """The API returned a refusal or an unusable response."""


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMRequest:
    """Bounded context sent to the extraction fallback."""

    filename: str
    excerpt: str
    unresolved: tuple[dict[str, Any], ...]
    allowed_fact_types: tuple[str, ...]
    constraints: tuple[str, ...]


@dataclass(frozen=True)
class LLMResponse:
    """Validated top-level proposal payload plus usage metadata."""

    proposals: tuple[dict[str, Any], ...]
    model: str
    usage: LLMUsage | None
    request_id: str | None = None


class LLMClient(Protocol):
    model: str

    def extract(self, request: LLMRequest) -> LLMResponse:
        """Return structured proposals for the supplied request."""


class OpenAILLMClient:
    """Official OpenAI SDK client using strict Structured Outputs."""

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        sdk_client: object | None = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("CONTRACTBREAKER_LLM_MODEL")
            or DEFAULT_LLM_MODEL
        ).strip()
        if not self.model:
            raise LLMUnavailable("invalid_model", "LLM model must not be empty")
        self._timeout = timeout
        if sdk_client is not None:
            self._client = sdk_client
            return

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMUnavailable(
                "missing_api_key", "OPENAI_API_KEY is not set"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable(
                "sdk_missing",
                "OpenAI SDK is not installed; install contractbreaker[llm]",
            ) from exc
        try:
            self._client = OpenAI(
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )
        except Exception as exc:
            raise LLMUnavailable(
                "client_init_failed",
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc

    def extract(self, request: LLMRequest) -> LLMResponse:
        payload = {
            "filename": request.filename,
            "source_excerpt_with_line_numbers": request.excerpt,
            "unresolved_facts": list(request.unresolved),
            "allowed_fact_types": list(request.allowed_fact_types),
            "constraints": list(request.constraints),
        }
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, sort_keys=True),
            },
        ]
        try:
            response = self._client.chat.completions.create(  # type: ignore[attr-defined]
                model=self.model,
                messages=messages,
                temperature=0,
                max_tokens=MAX_LLM_TOKENS,
                timeout=self._timeout,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "contractbreaker_extraction",
                        "strict": True,
                        "schema": _PROPOSAL_SCHEMA,
                    },
                },
            )
        except Exception as exc:
            raise LLMUnavailable(
                _classify_error(exc),
                f"{type(exc).__name__}: {str(exc)[:300]}",
            ) from exc

        try:
            choice = response.choices[0]
            message = choice.message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LLMInvalidOutput(
                    "refusal", "model refused the extraction request"
                )
            content = message.content
            if not isinstance(content, str) or not content.strip():
                raise LLMInvalidOutput(
                    "empty_output", "model returned no content"
                )
            parsed = json.loads(content)
        except LLMInvalidOutput:
            raise
        except Exception as exc:
            raise LLMInvalidOutput(
                "malformed_output",
                f"could not parse structured output: {type(exc).__name__}",
            ) from exc

        if not isinstance(parsed, dict) or set(parsed) != {"proposals"}:
            raise LLMInvalidOutput(
                "invalid_output", "structured output has invalid top-level fields"
            )
        proposals = parsed.get("proposals")
        if not isinstance(proposals, list) or any(
            not isinstance(item, dict) for item in proposals
        ):
            raise LLMInvalidOutput(
                "invalid_output", "structured output has an invalid proposals list"
            )

        usage = _usage_from_response(response)
        return LLMResponse(
            proposals=tuple(proposals),
            model=getattr(response, "model", self.model) or self.model,
            usage=usage,
            request_id=getattr(response, "id", None) or None,
        )


def create_default_llm_client(
    *, timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS
) -> OpenAILLMClient:
    return OpenAILLMClient(timeout=timeout)


def estimate_cost_usd(usage: LLMUsage | None) -> float | None:
    """Estimate cost only when explicit per-million rates are configured."""

    if usage is None:
        return None
    try:
        input_rate = float(
            os.environ["CONTRACTBREAKER_LLM_INPUT_USD_PER_1M"]
        )
        output_rate = float(
            os.environ["CONTRACTBREAKER_LLM_OUTPUT_USD_PER_1M"]
        )
    except (KeyError, ValueError):
        return None
    if input_rate < 0 or output_rate < 0:
        return None
    return round(
        usage.input_tokens / 1_000_000 * input_rate
        + usage.output_tokens / 1_000_000 * output_rate,
        8,
    )


def _usage_from_response(response: object) -> LLMUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (input_tokens, output_tokens, total_tokens)
    ):
        return None
    return LLMUsage(input_tokens, output_tokens, total_tokens)


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name in {"APITimeoutError", "TimeoutError"}:
        return "timeout"
    if name == "RateLimitError":
        return "rate_limit"
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return "authentication"
    if name == "APIConnectionError":
        return "connection"
    if name == "BadRequestError":
        return "bad_request"
    if name == "APIStatusError":
        return "api_status"
    if name == "OpenAIError":
        return "openai_error"
    return "api_error"