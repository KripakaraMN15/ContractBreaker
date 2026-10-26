"""Ordered event log. The causal check in scenarios.py reads this log, so a
finding is never based on "a test failed" alone."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


class EventLog:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._counter = itertools.count(1)

    def record(self, kind: str, **detail: Any) -> Event:
        event = Event(next(self._counter), kind, detail)
        self._events.append(event)
        return event

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def kinds(self) -> list[str]:
        return [e.kind for e in self._events]

    def count(self, kind: str) -> int:
        return sum(1 for e in self._events if e.kind == kind)
