"""Strict, deterministic causal verification for duplicate side effects.

A verdict is derived only from the ordered event trace and the observable
side-effect ledger. Event names are never treated as sufficient evidence:
every commit must belong to the expected attempt, and every step of the causal
chain must have a valid sequence position.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .events import Event, EventKind

_IDENTIFIED_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.REQUEST_RECEIVED,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.RESPONSE_DROPPED,
    EventKind.RESPONSE_RETURNED,
    EventKind.IDEMPOTENT_REPLAY,
}
_OPERATION_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.REQUEST_RECEIVED,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.IDEMPOTENT_REPLAY,
}
_TERMINAL_OUTCOMES = {"client_completed", "client_gave_up"}


class Verdict(str, Enum):
    """The three honest outcomes of a verification."""

    CONFIRMED = "confirmed"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Report:
    """A verdict plus the evidence it was based on."""

    verdict: Verdict
    reason: str
    side_effects: int
    events: tuple[Event, ...] = ()


def _result(
    verdict: Verdict, reason: str, side_effects: int, events: tuple[Event, ...]
) -> Report:
    return Report(verdict=verdict, reason=reason, side_effects=side_effects, events=events)


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _structure_error(trace: tuple[Event, ...]) -> str | None:
    previous = 0
    for event in trace:
        if isinstance(event.seq, bool) or not isinstance(event.seq, int):
            return f"event at position {event.seq!r} has a non-integer sequence"
        if event.seq <= previous:
            return f"sequence numbers are not strictly increasing at seq {event.seq}"
        previous = event.seq

    terminals = [event for event in trace if event.kind is EventKind.SCENARIO_END]
    if len(terminals) > 1:
        return "trace contains more than one scenario_end event"
    if terminals and terminals[0] is not trace[-1]:
        return "scenario_end is not the final event"
    if terminals:
        outcome = terminals[0].data.get("outcome")
        if outcome not in _TERMINAL_OUTCOMES:
            return f"scenario_end has an unknown outcome: {outcome!r}"
    return None


def verify_duplicate_side_effects(
    events: Iterable[Event], *, side_effects: int
) -> Report:
    """Verify the duplicate-side-effect causal chain.

    The required chain is:

    initial request -> initial commit -> dropped response -> retry request ->
    second commit caused by that retry.

    Each step must carry a matching attempt identifier, and all commits must
    reconcile with the observable side-effect count. Anything incomplete or
    contradictory is ``inconclusive``; it is never upgraded to ``confirmed``.
    """

    trace = tuple(events)

    def fail(reason: str) -> Report:
        return _result(Verdict.INCONCLUSIVE, reason, side_effects, trace)

    if isinstance(side_effects, bool) or not isinstance(side_effects, int):
        return _result(
            Verdict.INCONCLUSIVE, "side-effect count must be an integer", 0, trace
        )
    if side_effects < 0:
        return _result(
            Verdict.INCONCLUSIVE,
            "side-effect count can not be negative",
            side_effects,
            trace,
        )
    if not trace:
        return fail("event trace is empty")

    structure_error = _structure_error(trace)
    if structure_error is not None:
        return fail(structure_error)

    # Relevant events must identify the attempt they belong to. This is what
    # makes "a commit happened" different from "this retry committed".
    for event in trace:
        if event.kind not in _IDENTIFIED_KINDS:
            continue
        if not _has_text(event.attempt_id):
            return fail(f"{event.kind.value} event at seq {event.seq} has no attempt_id")
        if event.kind in _OPERATION_KINDS and not _has_text(event.operation_id):
            return fail(
                f"{event.kind.value} event at seq {event.seq} has no operation_id"
            )

    requests = [event for event in trace if event.kind is EventKind.REQUEST_SENT]
    received = [event for event in trace if event.kind is EventKind.REQUEST_RECEIVED]
    commits = [
        event for event in trace if event.kind is EventKind.SIDE_EFFECT_COMMITTED
    ]
    drops = [event for event in trace if event.kind is EventKind.RESPONSE_DROPPED]
    returned = [event for event in trace if event.kind is EventKind.RESPONSE_RETURNED]
    replays = [event for event in trace if event.kind is EventKind.IDEMPOTENT_REPLAY]

    if not requests:
        return fail("trace has no request_sent event")
    if len({event.operation_id for event in requests}) != 1:
        return fail("requests belong to more than one logical operation")
    operation_id = requests[0].operation_id

    request_by_attempt: dict[str, Event] = {}
    for event in requests:
        attempt = event.attempt_id or ""
        if attempt in request_by_attempt:
            return fail(f"attempt {attempt} sent more than one request")
        request_by_attempt[attempt] = event

    receipt_by_attempt: dict[str, Event] = {}
    for event in received:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"request_received refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(f"request_received for {attempt} has a mismatched operation_id")
        if event.seq <= request.seq:
            return fail(f"request_received for {attempt} precedes its request_sent")
        if attempt in receipt_by_attempt:
            return fail(f"attempt {attempt} was received more than once")
        receipt_by_attempt[attempt] = event

    commits_by_attempt: dict[str, list[Event]] = {}
    side_effect_ids: list[str] = []
    for event in commits:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"side_effect_committed refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(f"side_effect_committed for {attempt} has a mismatched operation_id")
        if event.seq <= request.seq:
            return fail(f"side_effect_committed for {attempt} precedes its request")
        side_effect_id = event.data.get("side_effect_id")
        if not _has_text(side_effect_id):
            return fail(f"side_effect_committed at seq {event.seq} has no side_effect_id")
        side_effect_ids.append(side_effect_id)
        commits_by_attempt.setdefault(attempt, []).append(event)

    if len(side_effect_ids) != len(set(side_effect_ids)):
        return fail("trace reuses a side-effect identifier")
    for attempt, attempt_commits in commits_by_attempt.items():
        if len(attempt_commits) > 1:
            return fail(f"attempt {attempt} committed more than one side effect")

    drop_by_attempt: dict[str, Event] = {}
    for event in drops:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"response_dropped refers to unknown attempt {attempt}")
        if attempt in drop_by_attempt:
            return fail(f"attempt {attempt} dropped its response more than once")
        attempt_commits = commits_by_attempt.get(attempt, [])
        if not attempt_commits:
            return fail(f"response_dropped for {attempt} has no committed side effect")
        if event.seq <= attempt_commits[-1].seq:
            return fail(f"response_dropped for {attempt} precedes its commit")
        drop_by_attempt[attempt] = event

    for event in returned:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"response_returned refers to unknown attempt {attempt}")
        if event.seq <= request.seq:
            return fail(f"response_returned for {attempt} precedes its request")

    replay_by_attempt: dict[str, Event] = {}
    for event in replays:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"idempotent_replay refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(f"idempotent_replay for {attempt} has a mismatched operation_id")
        if event.seq <= request.seq:
            return fail(f"idempotent_replay for {attempt} precedes its request")
        if attempt in replay_by_attempt:
            return fail(f"attempt {attempt} replayed more than once")
        replay_by_attempt[attempt] = event
        if attempt in commits_by_attempt:
            return fail(f"attempt {attempt} both replayed and committed a side effect")

    # The observable ledger must agree with the log. Aggregate counts alone
    # are not evidence, but a disagreement makes the evidence contradictory.
    if side_effects != len(commits):
        return fail(
            f"observed {side_effects} side effects but trace records {len(commits)} commits"
        )

    initial = requests[0]
    initial_attempt = initial.attempt_id or ""
    initial_commits = commits_by_attempt.get(initial_attempt, [])
    initial_drop = drop_by_attempt.get(initial_attempt)
    if len(initial_commits) != 1 or initial_drop is None:
        return fail("initial request has no single commit followed by a dropped response")

    initial_commit = initial_commits[0]
    if not initial.seq < initial_commit.seq < initial_drop.seq:
        return fail("initial request, commit, and dropped response are out of order")
    initial_receipt = receipt_by_attempt.get(initial_attempt)
    if initial_receipt is not None and not (
        initial.seq < initial_receipt.seq <= initial_commit.seq
    ):
        return fail("initial request_received is not positioned before its commit")

    retries = [event for event in requests if event.seq > initial_drop.seq]
    if not retries:
        terminal = next(
            (event for event in trace if event.kind is EventKind.SCENARIO_END), None
        )
        gave_up = terminal is not None and terminal.data.get("outcome") == "client_gave_up"
        if gave_up and len(commits) == 1:
            return _result(
                Verdict.NOT_REPRODUCED,
                "client did not retry after the lost response",
                side_effects,
                trace,
            )
        return fail("no retry is present and the trace does not show the client giving up")

    retry = retries[0]
    retry_attempt = retry.attempt_id or ""
    if retry.operation_id != operation_id:
        return fail("retry belongs to a different logical operation")

    retry_receipt = receipt_by_attempt.get(retry_attempt)
    retry_replay = replay_by_attempt.get(retry_attempt)
    retry_commits = commits_by_attempt.get(retry_attempt, [])

    if retry_commits:
        retry_commit = retry_commits[0]
        if _retry_order_invalid(retry, retry_commit, initial_drop, retry_receipt):
            return fail("retry request and second commit are out of causal order")
        if any(retry.seq < event.seq < retry_commit.seq for event in requests):
            return fail("another request occurred between the retry and its commit")
        if side_effects < 2:
            return fail("second commit is present but fewer than two side effects were observed")
        return _result(
            Verdict.CONFIRMED,
            (
                "retry after a lost response committed a second side effect "
                f"({side_effects} side effects for one logical operation)"
            ),
            side_effects,
            trace,
        )

    if retry_replay is not None:
        if not initial_drop.seq < retry.seq < retry_replay.seq:
            return fail("retry replay is not positioned after its request")
        if retry_receipt is not None and not (
            retry.seq < retry_receipt.seq <= retry_replay.seq
        ):
            return fail("retry request_received is not positioned before its replay")
        if any(retry.seq < event.seq < retry_replay.seq for event in requests):
            return fail("another request occurred between the retry and its replay")
        if len(commits) == 1:
            return _result(
                Verdict.NOT_REPRODUCED,
                "retry was served from the idempotency record; only the initial side effect occurred",
                side_effects,
                trace,
            )
        return fail("idempotent replay evidence contradicts the side-effect count")

    return fail("retry has neither a committed side effect nor idempotent replay evidence")


def _retry_order_invalid(
    retry: Event,
    retry_commit: Event,
    initial_drop: Event,
    retry_receipt: Event | None,
) -> bool:
    """Return whether the retry-to-commit window breaks causal ordering."""

    if not initial_drop.seq < retry.seq < retry_commit.seq:
        return True
    if retry_receipt is not None and not (
        retry.seq < retry_receipt.seq <= retry_commit.seq
    ):
        return True
    return False