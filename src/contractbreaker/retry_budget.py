"""Independent retry-budget verification over a runtime event trace.

The budget check never treats the client's configured retry count as a finding.
It compares a *declared* attempt budget, recorded in the trace by the harness,
against the attempts that were actually observed for one logical operation. Its
result is reported alongside the causal verdict and never replaces it: an
over-budget client can still have a confirmed duplicate-side-effect bug, and a
client that stays within budget is not thereby proven safe.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .events import Event, EventKind
from .observations import DEFAULT_SUPPORTED_PATH, collect_operation_attempts

OUTCOME_WITHIN_BUDGET = "within_budget"
OUTCOME_BUDGET_EXCEEDED = "budget_exceeded"
OUTCOME_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

_PROVENANCE_OUTCOME = "runtime_check"
_PROVENANCE_EXPECTED = "derived_from_extraction"
_PROVENANCE_OBSERVED = "runtime_observation"
_PROVENANCE_MISSING = "not_evaluated"


@dataclass(frozen=True)
class RetryBudgetResult:
    """The declared budget, the observed attempt count, and the comparison."""

    outcome: str
    expected_max_attempts: int | None
    observed_attempts: int | None
    reason: str
    counted_attempt_ids: tuple[str, ...] = ()
    ignored_attempt_ids: tuple[str, ...] = ()
    expected_source: str | None = None

    @property
    def exceeded(self) -> bool:
        """Whether the observed attempt count exceeded the declared budget."""

        return self.outcome == OUTCOME_BUDGET_EXCEEDED

    def to_dict(self) -> dict[str, object]:
        """Serialize for the report with per-field provenance."""

        return {
            "outcome": self.outcome,
            "expected_max_attempts": self.expected_max_attempts,
            "observed_attempts": self.observed_attempts,
            "counted_attempt_ids": list(self.counted_attempt_ids),
            "ignored_attempt_ids": list(self.ignored_attempt_ids),
            "provenance": {
                "outcome": _PROVENANCE_OUTCOME,
                "expected_max_attempts": (
                    _PROVENANCE_EXPECTED
                    if self.expected_max_attempts is not None
                    else _PROVENANCE_MISSING
                ),
                "observed_attempts": (
                    _PROVENANCE_OBSERVED
                    if self.observed_attempts is not None
                    else _PROVENANCE_MISSING
                ),
            },
            "reason": self.reason,
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _declared_budget(policy: Event) -> tuple[int | None, str | None]:
    """Return the declared attempt budget, or ``(None, None)`` if invalid.

    A contradiction between ``max_attempts`` and ``max_retries`` is treated as
    invalid rather than resolved in the check's favour.
    """

    data = policy.data
    max_retries = data.get("max_retries")
    if "max_attempts" in data:
        max_attempts = data["max_attempts"]
        if not _is_int(max_attempts) or max_attempts < 1:
            return None, None
        if (
            _is_int(max_retries)
            and max_retries >= 0
            and max_attempts != max_retries + 1
        ):
            return None, None
        return max_attempts, "max_attempts"
    if _is_int(max_retries) and max_retries >= 0:
        return max_retries + 1, "max_retries_plus_one"
    return None, None


def _insufficient(
    reason: str,
    *,
    expected: int | None = None,
    expected_source: str | None = None,
    ignored: tuple[str, ...] = (),
) -> RetryBudgetResult:
    return RetryBudgetResult(
        outcome=OUTCOME_INSUFFICIENT_EVIDENCE,
        expected_max_attempts=expected,
        observed_attempts=None,
        reason=reason,
        ignored_attempt_ids=ignored,
        expected_source=expected_source,
    )


def check_retry_budget(
    events: Iterable[Event],
    *,
    supported_path: str = DEFAULT_SUPPORTED_PATH,
) -> RetryBudgetResult:
    """Compare observed attempts with the declared attempt budget.

    Only ``request_sent`` events whose recorded path equals ``supported_path``
    count toward the budget. Unrelated or unsupported-path requests are listed
    as ignored, never counted. Missing, duplicated, contradictory, or
    uncorrelatable attempt identifiers produce ``insufficient_evidence`` rather
    than a passing result.
    """

    trace = tuple(events)
    if not trace:
        return _insufficient(
            "event trace is empty; the attempt count can not be established"
        )

    policies = [
        event for event in trace if event.kind is EventKind.RETRY_POLICY_CONFIGURED
    ]
    if len(policies) != 1:
        return _insufficient(
            "trace must contain exactly one retry budget declaration"
        )
    expected, expected_source = _declared_budget(policies[0])
    if expected is None:
        return _insufficient(
            "retry budget declaration has no valid, self-consistent "
            "max_attempts or max_retries value"
        )

    requests = [
        event for event in trace if event.kind is EventKind.REQUEST_SENT
    ]
    if not requests:
        return _insufficient(
            "trace has no request_sent event",
            expected=expected,
            expected_source=expected_source,
        )

    collected = collect_operation_attempts(trace, supported_path=supported_path)
    if collected.error is not None:
        return _insufficient(
            collected.error,
            expected=expected,
            expected_source=expected_source,
        )

    observed = len(collected.counted)
    counted_ids = tuple(str(event.attempt_id) for event in collected.counted)
    ignored_ids = tuple(str(event.attempt_id) for event in collected.ignored)
    suffix = (
        f"; {len(ignored_ids)} unrelated request(s) excluded from the budget"
        if ignored_ids
        else ""
    )

    if observed > expected:
        return RetryBudgetResult(
            outcome=OUTCOME_BUDGET_EXCEEDED,
            expected_max_attempts=expected,
            observed_attempts=observed,
            reason=(
                f"observed {observed} request attempt(s) exceeds the declared "
                f"maximum of {expected}{suffix}"
            ),
            counted_attempt_ids=counted_ids,
            ignored_attempt_ids=ignored_ids,
            expected_source=expected_source,
        )
    return RetryBudgetResult(
        outcome=OUTCOME_WITHIN_BUDGET,
        expected_max_attempts=expected,
        observed_attempts=observed,
        reason=(
            f"observed {observed} request attempt(s) within the declared "
            f"maximum of {expected}{suffix}"
        ),
        counted_attempt_ids=counted_ids,
        ignored_attempt_ids=ignored_ids,
        expected_source=expected_source,
    )
