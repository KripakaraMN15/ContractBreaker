"""Ordered events used as the only source of truth for causal verdicts.

Every HTTP attempt receives an explicit ``attempt_id``. Events that describe
work performed by that attempt carry the same identifier, which lets the
verifier prove that a commit belongs to the retry rather than to an unrelated
request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """Event types emitted by the reference client/server and fault adapter."""

    REQUEST_SENT = "request_sent"
    REQUEST_RECEIVED = "request_received"
    SIDE_EFFECT_COMMITTED = "side_effect_committed"
    RESPONSE_DROPPED = "response_dropped"
    RESPONSE_RETURNED = "response_returned"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    RETRY_POLICY_CONFIGURED = "retry_policy_configured"
    RETRYABLE_ERROR_RETURNED = "retryable_error_returned"
    RETRY_SCHEDULED = "retry_scheduled"
    SCENARIO_END = "scenario_end"


@dataclass(frozen=True)
class Event:
    """A single immutable event with a monotonic sequence number."""

    seq: int
    kind: EventKind
    attempt_id: str | None = None
    operation_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept strings so hand-built fixtures stay readable, but normalise
        # them to the enum before the verifier sees the event.
        if not isinstance(self.kind, EventKind):
            object.__setattr__(self, "kind", EventKind(self.kind))


class EventLog:
    """Append-only log that assigns strictly increasing sequence numbers."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._next_seq = 1

    def record(
        self,
        kind: EventKind,
        *,
        attempt_id: str | None = None,
        operation_id: str | None = None,
        **data: Any,
    ) -> Event:
        """Append an event and return it.

        Sequence numbers are allocated by the log, never by callers, so two
        events can not silently share a position in the trace.
        """

        event = Event(
            seq=self._next_seq,
            kind=kind,
            attempt_id=attempt_id,
            operation_id=operation_id,
            data=dict(data),
        )
        self._next_seq += 1
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[Event, ...]:
        """Return an immutable snapshot of the recorded events."""

        return tuple(self._events)
