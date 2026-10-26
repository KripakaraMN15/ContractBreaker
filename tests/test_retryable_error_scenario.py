"""Required milestone-3 tests for the retryable-HTTP-error scenario."""

from __future__ import annotations

from dataclasses import replace

import pytest

from contractbreaker import (
    EventKind,
    Report,
    Verdict,
    verify_retryable_error_duplicate,
)
from examples.payments.retryable_error_scenario import (
    RetryableErrorScenarioConfig,
    run_retryable_error_scenario,
)

CAUSAL_KINDS = {
    EventKind.REQUEST_SENT,
    EventKind.SIDE_EFFECT_COMMITTED,
    EventKind.RETRYABLE_ERROR_RETURNED,
    EventKind.RETRY_SCHEDULED,
}


def run_scenario(
    *,
    server_supports_idempotency: bool = False,
    client_sends_idempotency_key: bool = False,
    retry_statuses: frozenset[int] = frozenset({503}),
    max_retries: int = 1,
) -> Report:
    return run_retryable_error_scenario(
        RetryableErrorScenarioConfig(
            server_supports_idempotency=server_supports_idempotency,
            client_sends_idempotency_key=client_sends_idempotency_key,
            retry_statuses=retry_statuses,
            max_retries=max_retries,
        )
    )


def test_buggy_retryable_error_is_confirmed_with_two_side_effects() -> None:
    report = run_scenario()

    assert report.verdict is Verdict.CONFIRMED
    assert report.side_effects == 2

    causal = [event for event in report.events if event.kind in CAUSAL_KINDS]
    assert [event.kind for event in causal] == [
        EventKind.REQUEST_SENT,
        EventKind.SIDE_EFFECT_COMMITTED,
        EventKind.RETRYABLE_ERROR_RETURNED,
        EventKind.RETRY_SCHEDULED,
        EventKind.REQUEST_SENT,
        EventKind.SIDE_EFFECT_COMMITTED,
    ]
    assert [event.attempt_id for event in causal] == [
        "attempt-1",
        "attempt-1",
        "attempt-1",
        "attempt-1",
        "attempt-2",
        "attempt-2",
    ]
    assert len({event.operation_id for event in causal}) == 1
    commits = [
        event for event in report.events if event.kind is EventKind.SIDE_EFFECT_COMMITTED
    ]
    assert len({event.data["side_effect_id"] for event in commits}) == 2

    policy = next(
        event
        for event in report.events
        if event.kind is EventKind.RETRY_POLICY_CONFIGURED
    )
    assert tuple(policy.data["retry_statuses"]) == (503,)
    assert policy.data["max_retries"] == 1


def test_idempotency_fixed_retryable_error_has_one_side_effect() -> None:
    report = run_scenario(
        server_supports_idempotency=True,
        client_sends_idempotency_key=True,
    )

    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1
    commits = [
        event for event in report.events if event.kind is EventKind.SIDE_EFFECT_COMMITTED
    ]
    assert len(commits) == 1
    replay = [
        event for event in report.events if event.kind is EventKind.IDEMPOTENT_REPLAY
    ]
    assert len(replay) == 1
    assert replay[0].attempt_id == "attempt-2"
    assert replay[0].operation_id == commits[0].operation_id


@pytest.mark.parametrize(
    "config",
    [
        {"retry_statuses": frozenset()},
        {"retry_statuses": frozenset({500})},
        {"max_retries": 0},
    ],
    ids=["empty-policy", "status-not-in-policy", "no-retry-budget"],
)
def test_explicit_no_retry_policy_is_not_reproduced(
    config: dict[str, object],
) -> None:
    report = run_scenario(**config)  # type: ignore[arg-type]

    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1
    assert [
        event for event in report.events if event.kind is EventKind.RETRY_SCHEDULED
    ] == []
    assert len(
        [
            event
            for event in report.events
            if event.kind is EventKind.REQUEST_SENT
        ]
    ) == 1
    assert any(
        event.kind is EventKind.RETRY_POLICY_CONFIGURED for event in report.events
    )


def test_missing_retryable_error_event_is_inconclusive() -> None:
    report = run_scenario()
    trace = [
        event
        for event in report.events
        if event.kind is not EventKind.RETRYABLE_ERROR_RETURNED
    ]

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is Verdict.INCONCLUSIVE


def test_missing_retry_policy_event_is_inconclusive() -> None:
    report = run_scenario()
    trace = [
        event
        for event in report.events
        if event.kind is not EventKind.RETRY_POLICY_CONFIGURED
    ]

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is Verdict.INCONCLUSIVE


def test_retry_without_recorded_policy_decision_is_not_confirmed() -> None:
    report = run_scenario()
    trace = [
        event
        for event in report.events
        if event.kind is not EventKind.RETRY_SCHEDULED
    ]

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is not Verdict.CONFIRMED
    assert result.verdict is Verdict.INCONCLUSIVE


def test_retry_decision_without_retry_request_is_inconclusive() -> None:
    report = run_scenario()
    decision_index = next(
        index
        for index, event in enumerate(report.events)
        if event.kind is EventKind.RETRY_SCHEDULED
    )
    trace = list(report.events[: decision_index + 1])
    trace.append(replace(report.events[-1], seq=report.events[-1].seq + 10))

    result = verify_retryable_error_duplicate(trace, side_effects=1)

    assert result.verdict is Verdict.INCONCLUSIVE


def test_incorrect_commit_and_error_ordering_is_inconclusive() -> None:
    report = run_scenario()
    trace = list(report.events)
    commit_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.SIDE_EFFECT_COMMITTED
        and event.attempt_id == "attempt-1"
    )
    error_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.RETRYABLE_ERROR_RETURNED
    )
    trace[commit_index], trace[error_index] = trace[error_index], trace[commit_index]

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is not Verdict.CONFIRMED
    assert result.verdict is Verdict.INCONCLUSIVE


def test_mismatched_retry_attempt_identifier_is_not_confirmed() -> None:
    report = run_scenario()
    trace = list(report.events)
    decision_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.RETRY_SCHEDULED
    )
    trace[decision_index] = replace(
        trace[decision_index], attempt_id="attempt-2"
    )

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is not Verdict.CONFIRMED
    assert result.verdict is Verdict.INCONCLUSIVE


def test_retry_decision_must_match_configured_policy() -> None:
    report = run_scenario()
    trace = list(report.events)
    decision_index = next(
        index
        for index, event in enumerate(trace)
        if event.kind is EventKind.RETRY_SCHEDULED
    )
    trace[decision_index] = replace(
        trace[decision_index],
        data={
            **trace[decision_index].data,
            "retry_statuses": (500,),
        },
    )

    result = verify_retryable_error_duplicate(trace, side_effects=2)

    assert result.verdict is Verdict.INCONCLUSIVE