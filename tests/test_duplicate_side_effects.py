"""Required milestone-1 scenarios for the strict causal verifier."""

from __future__ import annotations

from dataclasses import replace

import pytest

from contractbreaker import (
    EventKind,
    EventLog,
    Report,
    Verdict,
    verify_duplicate_side_effects,
)
from examples.payments.scenario import PaymentScenarioConfig, run_payment_scenario

CAUSAL_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.RESPONSE_DROPPED,
}


def run_scenario(
    *,
    server_supports_idempotency: bool,
    client_sends_idempotency_key: bool,
    max_retries: int = 1,
) -> Report:
    return run_payment_scenario(
        PaymentScenarioConfig(
            server_supports_idempotency=server_supports_idempotency,
            client_sends_idempotency_key=client_sends_idempotency_key,
            max_retries=max_retries,
        )
    )


def test_buggy_client_and_server_confirm_two_side_effects() -> None:
    report = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )

    assert report.verdict is Verdict.CONFIRMED
    assert report.side_effects == 2

    causal_events = [event for event in report.events if event.kind in CAUSAL_KINDS]
    assert [event.kind for event in causal_events] == [
        EventKind.REQUEST_SENT,
        EventKind.SIDE_EFFECT_COMMITTED,
        EventKind.RESPONSE_DROPPED,
        EventKind.REQUEST_SENT,
        EventKind.SIDE_EFFECT_COMMITTED,
    ]
    assert [event.attempt_id for event in causal_events] == [
        "attempt-1",
        "attempt-1",
        "attempt-1",
        "attempt-2",
        "attempt-2",
    ]


def test_idempotency_protection_on_both_sides_is_not_reproduced() -> None:
    report = run_scenario(
        server_supports_idempotency=True,
        client_sends_idempotency_key=True,
    )

    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1
    assert sum(
        event.kind is EventKind.SIDE_EFFECT_COMMITTED for event in report.events
    ) == 1
    replay = [event for event in report.events if event.kind is EventKind.IDEMPOTENT_REPLAY]
    assert len(replay) == 1
    assert replay[0].attempt_id == "attempt-2"


def test_client_that_never_retries_is_not_reproduced() -> None:
    report = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
        max_retries=0,
    )

    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1
    assert [event.attempt_id for event in report.events if event.kind is EventKind.REQUEST_SENT] == [
        "attempt-1"
    ]


def test_server_only_idempotency_still_duplicates() -> None:
    report = run_scenario(
        server_supports_idempotency=True,
        client_sends_idempotency_key=False,
    )

    assert report.verdict is Verdict.CONFIRMED
    assert report.side_effects == 2


@pytest.mark.parametrize(
    "remove_kind",
    [
        EventKind.RESPONSE_DROPPED,
        EventKind.SIDE_EFFECT_COMMITTED,
    ],
)
def test_incomplete_event_trace_is_inconclusive(remove_kind: EventKind) -> None:
    complete = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )
    assert complete.verdict is Verdict.CONFIRMED

    incomplete = tuple(
        event for event in complete.events if event.kind is not remove_kind
    )
    report = verify_duplicate_side_effects(incomplete, side_effects=2)

    assert report.verdict is Verdict.INCONCLUSIVE


def test_incorrect_event_ordering_does_not_confirm() -> None:
    complete = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )
    trace = list(complete.events)
    commit_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.SIDE_EFFECT_COMMITTED
        and event.attempt_id == "attempt-1"
    )
    drop_index = next(
        index for index, event in enumerate(trace) if event.kind is EventKind.RESPONSE_DROPPED
    )
    trace[commit_index], trace[drop_index] = trace[drop_index], trace[commit_index]

    report = verify_duplicate_side_effects(trace, side_effects=2)

    assert report.verdict is not Verdict.CONFIRMED
    assert report.verdict is Verdict.INCONCLUSIVE


def test_mismatched_attempt_identifiers_do_not_confirm() -> None:
    complete = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )
    trace = list(complete.events)
    second_commit_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.SIDE_EFFECT_COMMITTED
        and event.attempt_id == "attempt-2"
    )
    trace[second_commit_index] = replace(trace[second_commit_index], attempt_id="attempt-1")

    report = verify_duplicate_side_effects(trace, side_effects=2)

    assert report.verdict is not Verdict.CONFIRMED
    assert report.verdict is Verdict.INCONCLUSIVE


def test_duplicate_sequence_numbers_are_inconclusive() -> None:
    complete = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )
    trace = list(complete.events)
    trace[1] = replace(trace[1], seq=trace[0].seq)

    report = verify_duplicate_side_effects(trace, side_effects=2)

    assert report.verdict is Verdict.INCONCLUSIVE


def test_ledger_and_event_log_disagreement_is_inconclusive() -> None:
    complete = run_scenario(
        server_supports_idempotency=False,
        client_sends_idempotency_key=False,
    )

    report = verify_duplicate_side_effects(complete.events, side_effects=1)

    assert report.verdict is Verdict.INCONCLUSIVE


def test_event_log_assigns_strictly_increasing_sequence_numbers() -> None:
    log = EventLog()
    first = log.record(EventKind.REQUEST_SENT, attempt_id="attempt-1", operation_id="op")
    second = log.record(EventKind.REQUEST_RECEIVED, attempt_id="attempt-1", operation_id="op")

    assert first.seq == 1
    assert second.seq == 2
    assert log.events == (first, second)


def test_replay_and_commit_for_same_retry_are_contradictory() -> None:
    log = EventLog()
    log.record(EventKind.REQUEST_SENT, attempt_id="attempt-1", operation_id="op")
    log.record(EventKind.REQUEST_RECEIVED, attempt_id="attempt-1", operation_id="op")
    log.record(
        EventKind.SIDE_EFFECT_COMMITTED,
        attempt_id="attempt-1",
        operation_id="op",
        side_effect_id="effect-1",
    )
    log.record(EventKind.RESPONSE_DROPPED, attempt_id="attempt-1", operation_id="op")
    log.record(EventKind.REQUEST_SENT, attempt_id="attempt-2", operation_id="op")
    log.record(EventKind.REQUEST_RECEIVED, attempt_id="attempt-2", operation_id="op")
    log.record(EventKind.IDEMPOTENT_REPLAY, attempt_id="attempt-2", operation_id="op")
    log.record(
        EventKind.SIDE_EFFECT_COMMITTED,
        attempt_id="attempt-2",
        operation_id="op",
        side_effect_id="effect-2",
    )
    log.record(EventKind.SCENARIO_END, outcome="client_completed")

    report = verify_duplicate_side_effects(log.events, side_effects=2)

    assert report.verdict is Verdict.INCONCLUSIVE