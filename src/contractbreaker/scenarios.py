"""Scenario runner with three honest outcomes:

CONFIRMED       the predicted causal sequence happened AND the invariant broke
NOT_REPRODUCED  the scenario ran as designed but the failure did not occur
INCONCLUSIVE    the harness could not establish the preconditions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import requests

from .events import EventLog
from .harness import FaultInjectingAdapter, FaultPlan

BASE_URL = "http://service.test"


class Verdict(str, Enum):
    CONFIRMED = "confirmed"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"


@dataclass
class Report:
    verdict: Verdict
    reason: str
    side_effects: int
    event_kinds: list[str] = field(default_factory=list)


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(item in it for item in needle)


def run_lost_response_scenario(
    make_app: Callable[[list, EventLog], object],
    make_client: Callable[[requests.Session], object],
    call: Callable[[object], object],
) -> Report:
    """Invariant under test: one logical operation produces exactly one side
    effect, even when the first response is lost after the server committed."""
    log = EventLog()
    effects: list = []
    app = make_app(effects, log)

    session = requests.Session()
    session.mount(BASE_URL, FaultInjectingAdapter(app, log, FaultPlan({1})))
    client = make_client(session)

    try:
        call(client)
    except Exception as exc:  # the client may legitimately give up
        log.record("client_error", type=type(exc).__name__)

    kinds = log.kinds()
    n = len(effects)

    # Preconditions: first request committed, then its response was lost.
    if not _is_subsequence(["request_sent", "side_effect_committed", "response_dropped"], kinds):
        return Report(Verdict.INCONCLUSIVE, "precondition not met: no commit-then-lost-response", n, kinds)

    after_drop = kinds[kinds.index("response_dropped") + 1 :]
    if "request_sent" not in after_drop:
        return Report(Verdict.NOT_REPRODUCED, "client did not retry after the lost response", n, kinds)

    if n >= 2 and "side_effect_committed" in after_drop:
        return Report(
            Verdict.CONFIRMED,
            f"retry after lost response caused {n} side effects for one logical operation",
            n,
            kinds,
        )
    return Report(Verdict.NOT_REPRODUCED, "client retried but the server deduplicated the side effect", n, kinds)
