"""Strict causal verification for duplicate commits after a retryable error.

This is a distinct scenario from the lost-response case. The server commits,
returns an HTTP error selected by an explicitly configured client retry policy,
and the client retries only after recording a retry decision for that error.
A duplicate is confirmed only when the full ordered chain proves all three
conditions.
"""

from __future__ import annotations

from typing import Iterable

from .events import Event, EventKind
from .verifier import Report, Verdict

_IDENTIFIED_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.REQUEST_RECEIVED,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.RETRYABLE_ERROR_RETURNED,
    EventKind.RESPONSE_RETURNED,
    EventKind.RETRY_SCHEDULED,
    EventKind.IDEMPOTENT_REPLAY,
}
_OPERATION_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.REQUEST_RECEIVED,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.RETRYABLE_ERROR_RETURNED,
    EventKind.RETRY_SCHEDULED,
    EventKind.IDEMPOTENT_REPLAY,
}
_TERMINAL_OUTCOMES = {"client_completed", "client_gave_up"}


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _status_set(
    value: object, *, allow_empty: bool = False
) -> frozenset[int] | None:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return None
    statuses: list[int] = []
    for status in value:
        if not _is_int(status) or not 400 <= status <= 599:
            return None
        statuses.append(status)
    if not statuses and not allow_empty:
        return None
    return frozenset(statuses)


def _result(
    verdict: Verdict, reason: str, side_effects: int, trace: tuple[Event, ...]
) -> Report:
    return Report(
        verdict=verdict,
        reason=reason,
        side_effects=side_effects,
        events=trace,
    )


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


def verify_retryable_error_duplicate(
    events: Iterable[Event], *, side_effects: int
) -> Report:
    """Verify a duplicate caused by retrying an explicitly retryable error.

    The required chain is:

    request(A) -> commit(A) -> retryable error(A) -> retry decision(A)
    -> request(B) -> commit(B)

    A retry request without a recorded policy decision is inconclusive, even
    when the status looks retryable. The function never assumes that a client
    retries a 5xx response.
    """

    trace = tuple(events)

    def fail(reason: str) -> Report:
        return _result(Verdict.INCONCLUSIVE, reason, side_effects, trace)

    if isinstance(side_effects, bool) or not isinstance(side_effects, int):
        return _result(
            Verdict.INCONCLUSIVE, "side-effect count must be an integer", 0, trace
        )
    if side_effects < 0:
        return fail("side-effect count can not be negative")
    if not trace:
        return fail("event trace is empty")

    structure_error = _structure_error(trace)
    if structure_error is not None:
        return fail(structure_error)

    policies = [
        event for event in trace if event.kind is EventKind.RETRY_POLICY_CONFIGURED
    ]
    if len(policies) != 1:
        return fail("trace must contain exactly one retry policy configuration")
    policy = policies[0]
    policy_statuses = _status_set(
        policy.data.get("retry_statuses"), allow_empty=True
    )
    if policy_statuses is None:
        return fail("retry policy has no valid configured HTTP error statuses")
    max_retries = policy.data.get("max_retries")
    if not _is_int(max_retries) or max_retries < 0:
        return fail("retry policy has an invalid max_retries value")

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
    received = [
        event for event in trace if event.kind is EventKind.REQUEST_RECEIVED
    ]
    commits = [
        event for event in trace if event.kind is EventKind.SIDE_EFFECT_COMMITTED
    ]
    errors = [
        event
        for event in trace
        if event.kind is EventKind.RETRYABLE_ERROR_RETURNED
    ]
    decisions = [
        event for event in trace if event.kind is EventKind.RETRY_SCHEDULED
    ]
    returned = [
        event for event in trace if event.kind is EventKind.RESPONSE_RETURNED
    ]
    replays = [
        event for event in trace if event.kind is EventKind.IDEMPOTENT_REPLAY
    ]

    if not requests:
        return fail("trace has no request_sent event")
    operations = {event.operation_id for event in requests}
    if len(operations) != 1:
        return fail("requests belong to more than one logical operation")
    operation_id = requests[0].operation_id

    request_by_attempt: dict[str, Event] = {}
    for event in requests:
        attempt = event.attempt_id or ""
        if attempt in request_by_attempt:
            return fail(f"attempt {attempt} sent more than one request")
        request_by_attempt[attempt] = event
    if policy.seq > requests[0].seq:
        return fail("retry policy was configured after the initial request")

    receipt_by_attempt: dict[str, Event] = {}
    for event in received:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"request_received refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(f"request_received for {attempt} has a mismatched operation_id")
        if event.seq <= request.seq:
            return fail(f"request_received for {attempt} precedes its request")
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
            return fail(
                f"side_effect_committed for {attempt} has a mismatched operation_id"
            )
        if event.seq <= request.seq:
            return fail(f"side_effect_committed for {attempt} precedes its request")
        side_effect_id = event.data.get("side_effect_id")
        if not _has_text(side_effect_id):
            return fail(
                f"side_effect_committed at seq {event.seq} has no side_effect_id"
            )
        side_effect_ids.append(side_effect_id)
        commits_by_attempt.setdefault(attempt, []).append(event)
    if len(side_effect_ids) != len(set(side_effect_ids)):
        return fail("trace reuses a side-effect identifier")
    for attempt, attempt_commits in commits_by_attempt.items():
        if len(attempt_commits) > 1:
            return fail(f"attempt {attempt} committed more than one side effect")

    error_by_attempt: dict[str, Event] = {}
    for event in errors:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"retryable error refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(
                f"retryable error for {attempt} has a mismatched operation_id"
            )
        if attempt in error_by_attempt:
            return fail(f"attempt {attempt} has more than one retryable error")
        status = event.data.get("status_code")
        if not _is_int(status) or not 400 <= status <= 599:
            return fail(f"retryable error for {attempt} has an invalid status_code")
        error_by_attempt[attempt] = event

    decision_by_attempt: dict[str, Event] = {}
    for event in decisions:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"retry decision refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(
                f"retry decision for {attempt} has a mismatched operation_id"
            )
        if attempt in decision_by_attempt:
            return fail(f"attempt {attempt} scheduled more than one retry")
        statuses = _status_set(event.data.get("retry_statuses"))
        if statuses is None:
            return fail(f"retry decision for {attempt} has invalid configured statuses")
        if statuses != policy_statuses:
            return fail(
                f"retry decision for {attempt} does not match the configured policy"
            )
        status = event.data.get("status_code")
        if not _is_int(status) or status not in policy_statuses:
            return fail(
                f"retry decision for {attempt} is not for a configured retryable status"
            )
        decision_by_attempt[attempt] = event

    replay_by_attempt: dict[str, Event] = {}
    for event in replays:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"idempotent_replay refers to unknown attempt {attempt}")
        if event.operation_id != request.operation_id:
            return fail(
                f"idempotent_replay for {attempt} has a mismatched operation_id"
            )
        if event.seq <= request.seq:
            return fail(f"idempotent_replay for {attempt} precedes its request")
        if attempt in replay_by_attempt:
            return fail(f"attempt {attempt} replayed more than once")
        replay_by_attempt[attempt] = event
        if attempt in commits_by_attempt:
            return fail(f"attempt {attempt} both replayed and committed a side effect")

    for event in returned:
        attempt = event.attempt_id or ""
        request = request_by_attempt.get(attempt)
        if request is None:
            return fail(f"response_returned refers to unknown attempt {attempt}")
        if event.seq <= request.seq:
            return fail(f"response_returned for {attempt} precedes its request")

    if side_effects != len(commits):
        return fail(
            f"observed {side_effects} side effects but trace records {len(commits)} commits"
        )

    initial = requests[0]
    initial_attempt = initial.attempt_id or ""
    initial_commits = commits_by_attempt.get(initial_attempt, [])
    initial_error = error_by_attempt.get(initial_attempt)
    if len(initial_commits) != 1 or initial_error is None:
        return fail(
            "initial request has no single commit followed by a retryable error"
        )
    initial_commit = initial_commits[0]
    if not initial.seq < initial_commit.seq < initial_error.seq:
        return fail(
            "initial request, commit, and retryable error are out of order"
        )
    initial_receipt = receipt_by_attempt.get(initial_attempt)
    if initial_receipt is not None and not (
        initial.seq < initial_receipt.seq <= initial_commit.seq
    ):
        return fail("initial request_received is not positioned before its commit")

    initial_decision = decision_by_attempt.get(initial_attempt)
    if initial_decision is None:
        later_requests = [event for event in requests if event.seq > initial_error.seq]
        if later_requests:
            return fail(
                "a retry request is present without a recorded retry-policy decision"
            )
        terminal = next(
            (event for event in trace if event.kind is EventKind.SCENARIO_END), None
        )
        gave_up = terminal is not None and terminal.data.get("outcome") == "client_gave_up"
        if gave_up and len(commits) == 1:
            return _result(
                Verdict.NOT_REPRODUCED,
                "client did not retry the configured retryable error",
                side_effects,
                trace,
            )
        return fail(
            "no retry decision is present and the trace does not show the client giving up"
        )

    error_status = initial_error.data.get("status_code")
    decision_status = initial_decision.data.get("status_code")
    if decision_status != error_status:
        return fail("retry decision status does not match the retryable error")
    if error_status not in policy_statuses:
        return fail("retryable error status was not configured as retryable")
    if max_retries < 1:
        return fail("retry policy allows no retry but a retry decision was recorded")
    if not initial_error.seq < initial_decision.seq:
        return fail("retry decision precedes the retryable error")
    initial_return = next(
        (
            event
            for event in returned
            if event.attempt_id == initial_attempt
        ),
        None,
    )
    if initial_return is not None and not (
        initial_error.seq < initial_return.seq < initial_decision.seq
    ):
        return fail("response_returned is not positioned between error and retry decision")
    if any(
        initial_error.seq < event.seq < initial_decision.seq for event in requests
    ):
        return fail("a retry request precedes the recorded retry decision")

    retries = [event for event in requests if event.seq > initial_decision.seq]
    if not retries:
        return fail("retry decision is present but no retry request followed")
    retry = retries[0]
    retry_attempt = retry.attempt_id or ""
    if retry_attempt == initial_attempt:
        return fail("retry reused the initial attempt identifier")
    if retry.operation_id != operation_id:
        return fail("retry belongs to a different logical operation")

    retry_commits = commits_by_attempt.get(retry_attempt, [])
    retry_replay = replay_by_attempt.get(retry_attempt)
    if retry_commits and retry_replay is not None:
        return fail("retry both replayed and committed a side effect")

    if retry_commits:
        retry_commit = retry_commits[0]
        if not retry.seq < retry_commit.seq:
            return fail("retry commit does not follow the retry request")
        if any(
            retry.seq < event.seq < retry_commit.seq for event in requests
        ):
            return fail("another request occurred between the retry and its commit")
        if side_effects < 2:
            return fail("second commit is present but fewer than two side effects exist")
        return _result(
            Verdict.CONFIRMED,
            (
                "retry of an explicitly configured retryable error "
                f"committed a second side effect ({side_effects} side effects)"
            ),
            side_effects,
            trace,
        )

    if retry_replay is not None:
        if not retry.seq < retry_replay.seq:
            return fail("idempotent replay does not follow the retry request")
        if any(
            retry.seq < event.seq < retry_replay.seq for event in requests
        ):
            return fail("another request occurred between the retry and its replay")
        if len(commits) == 1 and side_effects == 1:
            return _result(
                Verdict.NOT_REPRODUCED,
                "retry was deduplicated by the server idempotency record",
                side_effects,
                trace,
            )
        return fail("idempotent replay evidence contradicts the side-effect count")

    return fail("retry has neither a committed side effect nor idempotent replay evidence")