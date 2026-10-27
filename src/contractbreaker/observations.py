"""Shared helpers for reading per-attempt runtime observations.

Both the retry-budget check and the idempotency-key check need the same thing:
the set of request attempts that provably belong to one logical operation for
the tested endpoint. Keeping that logic here means the evidence rules are
enforced once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from .events import Event, EventKind

DEFAULT_SUPPORTED_PATH = "/payments"


@dataclass(frozen=True)
class OperationAttempts:
    """Attempts attributable to the tested endpoint, or why they are not."""

    counted: tuple[Event, ...] = ()
    ignored: tuple[Event, ...] = ()
    error: str | None = None


def key_fingerprint(value: str) -> str:
    """Return a stable, non-reversible fingerprint of an idempotency key.

    Reports never carry the raw header value, only this fingerprint, so key
    comparison is possible without disclosing whatever the client sent.
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def collect_operation_attempts(
    events: Iterable[Event],
    *,
    supported_path: str = DEFAULT_SUPPORTED_PATH,
) -> OperationAttempts:
    """Collect countable requests for one logical operation.

    A request counts only when it carries a unique, present attempt identifier,
    an operation identifier, and a recorded path equal to ``supported_path``.
    Every request must share one operation. Anything else returns ``error`` so
    callers report insufficient evidence instead of a passing result.
    """

    requests = [
        event for event in events if event.kind is EventKind.REQUEST_SENT
    ]
    if not requests:
        return OperationAttempts(error="trace has no request_sent event")

    seen: set[str] = set()
    for event in requests:
        if not _has_text(event.attempt_id):
            return OperationAttempts(
                error=f"request_sent at seq {event.seq} has no attempt identifier"
            )
        if not _has_text(event.operation_id):
            return OperationAttempts(
                error=f"request_sent at seq {event.seq} has no operation identifier"
            )
        attempt_id = str(event.attempt_id)
        if attempt_id in seen:
            return OperationAttempts(
                error=(
                    f"attempt identifier {attempt_id} is used by more than one "
                    "request"
                )
            )
        seen.add(attempt_id)

    counted: list[Event] = []
    ignored: list[Event] = []
    for event in requests:
        path = event.data.get("path")
        if not _has_text(path):
            return OperationAttempts(
                error=(
                    f"request_sent at seq {event.seq} records no path, so the "
                    "attempt can not be attributed to the tested endpoint"
                )
            )
        if path == supported_path:
            counted.append(event)
        else:
            ignored.append(event)

    if not counted:
        return OperationAttempts(
            ignored=tuple(ignored),
            error=f"no request targeted the supported path {supported_path}",
        )
    if len({event.operation_id for event in counted}) != 1:
        return OperationAttempts(
            ignored=tuple(ignored),
            error="counted attempts belong to more than one logical operation",
        )
    return OperationAttempts(counted=tuple(counted), ignored=tuple(ignored))
