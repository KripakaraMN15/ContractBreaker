"""Typed, JSON-serializable results for deterministic source extraction.

The extraction model deliberately separates a confirmed absence (``ABSENT``)
from a value that could not be determined (``UNKNOWN``). Consumers should
never fill a missing timeout or retry limit with a default; they should inspect
``Fact.status`` and ``Fact.reason``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ExtractionStatus(str, Enum):
    """Overall outcome for one source file."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    NO_HTTP_CALLS = "no_http_calls"
    PARSE_ERROR = "parse_error"


class FactStatus(str, Enum):
    """Whether a fact is known, confirmed absent, or undetermined."""

    KNOWN = "known"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SourceLocation:
    """Human-readable source span using 1-based line and column numbers."""

    file: str
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Fact(Generic[T]):
    """A value plus an explicit reason when it is not known."""

    status: FactStatus
    value: T | None = None
    reason: str | None = None

    @classmethod
    def known(cls, value: T, reason: str | None = None) -> "Fact[T]":
        return cls(FactStatus.KNOWN, value=value, reason=reason)

    @classmethod
    def absent(cls, reason: str) -> "Fact[T]":
        return cls(FactStatus.ABSENT, reason=reason)

    @classmethod
    def unknown(cls, reason: str, value: T | None = None) -> "Fact[T]":
        return cls(FactStatus.UNKNOWN, value=value, reason=reason)

    @classmethod
    def unsupported(cls, reason: str, value: T | None = None) -> "Fact[T]":
        return cls(FactStatus.UNSUPPORTED, value=value, reason=reason)

    @classmethod
    def ambiguous(cls, reason: str, value: T | None = None) -> "Fact[T]":
        return cls(FactStatus.AMBIGUOUS, value=value, reason=reason)


@dataclass(frozen=True)
class HeaderFact:
    """One HTTP header entry found in a request call."""

    name: Fact[str]
    value: Fact[str]
    location: SourceLocation


@dataclass(frozen=True)
class IdempotencyKeyFact:
    """How an Idempotency-Key was supplied across retry attempts."""

    header_name: str
    key_expression: str | None
    generated_inside_retry: Fact[bool]
    reused_across_attempts: Fact[bool]
    location: SourceLocation


@dataclass(frozen=True)
class HttpCallFact:
    """A recognized requests HTTP call."""

    location: SourceLocation
    method: Fact[str]
    endpoint: Fact[str]
    timeout: Fact[int | float | None]
    headers: tuple[HeaderFact, ...]
    idempotency_key: Fact[IdempotencyKeyFact]
    retry_loop: SourceLocation | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryLoopFact:
    """A recognized explicit retry loop.

    ``max_attempts`` is the number of loop iterations. ``retry_count`` is the
    derived number of retries after the first attempt (``max_attempts - 1``).
    The two are reported separately so callers do not confuse them.
    """

    location: SourceLocation
    loop_expression: str
    max_attempts: Fact[int]
    retry_count: Fact[int]
    triggers: Fact[tuple[str, ...]]
    http_call_locations: tuple[SourceLocation, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionWarning:
    """An unsupported or ambiguous pattern that limits confidence."""

    code: str
    message: str
    location: SourceLocation | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """All facts extracted from one Python file."""

    file: str
    status: ExtractionStatus
    calls: tuple[HttpCallFact, ...]
    retry_loops: tuple[RetryLoopFact, ...]
    warnings: tuple[ExtractionWarning, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary; ``json.dumps`` can serialize it as-is."""

        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize the result to JSON."""

        return json.dumps(self.to_dict(), indent=indent)