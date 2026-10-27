"""Runtime verification of idempotency-key stability across retry attempts.

The check compares the key fingerprint recorded for each attempt of one logical
operation. It never treats the AST prediction as proof of the runtime value: the
static expectation is carried separately, with its own provenance, so the two
can disagree visibly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .events import Event
from .observations import DEFAULT_SUPPORTED_PATH, collect_operation_attempts

OUTCOME_STABLE = "stable"
OUTCOME_CHANGING = "changing"
OUTCOME_ABSENT = "absent"
OUTCOME_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

_PROVENANCE_OUTCOME = "runtime_check"
_PROVENANCE_OBSERVED = "runtime_observation"
_PROVENANCE_STATIC = "derived_from_extraction"
_PROVENANCE_MISSING = "not_evaluated"


@dataclass(frozen=True)
class IdempotencyKeyResult:
    """Observed key behaviour, plus the static expectation it can contradict."""

    outcome: str
    reason: str
    attempts: tuple[dict[str, object], ...] = ()
    distinct_key_count: int | None = None
    static_expectation: dict[str, object] | None = None
    matches_static_expectation: bool | None = None

    @property
    def stable(self) -> bool:
        """Whether every attempt sent the same key value."""

        return self.outcome == OUTCOME_STABLE

    def to_dict(self) -> dict[str, object]:
        """Serialize for the report without exposing raw key values."""

        return {
            "outcome": self.outcome,
            "attempts": [dict(attempt) for attempt in self.attempts],
            "distinct_key_count": self.distinct_key_count,
            "static_expectation": self.static_expectation,
            "matches_static_expectation": self.matches_static_expectation,
            "provenance": {
                "outcome": _PROVENANCE_OUTCOME,
                "observed_keys": (
                    _PROVENANCE_OBSERVED
                    if self.attempts
                    else _PROVENANCE_MISSING
                ),
                "static_expectation": (
                    _PROVENANCE_STATIC
                    if self.static_expectation is not None
                    else _PROVENANCE_MISSING
                ),
            },
            "reason": self.reason,
        }


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _static_expectation(
    key_present: bool | None, key_reused: bool | None
) -> dict[str, object] | None:
    if key_present is None:
        return None
    expectation: dict[str, object] = {
        "key_present": bool(key_present),
        "reused_across_attempts": None,
        "provenance": _PROVENANCE_STATIC,
    }
    if key_present and key_reused is not None:
        expectation["reused_across_attempts"] = bool(key_reused)
    return expectation


def _matches_static(
    outcome: str, expectation: dict[str, object] | None
) -> bool | None:
    if expectation is None:
        return None
    if not expectation["key_present"]:
        return outcome == OUTCOME_ABSENT
    if outcome == OUTCOME_ABSENT:
        return False
    if outcome == OUTCOME_INSUFFICIENT_EVIDENCE:
        return None
    reused = expectation["reused_across_attempts"]
    if reused is None:
        return None
    return (outcome == OUTCOME_STABLE) == bool(reused)


def _insufficient(
    reason: str,
    expectation: dict[str, object] | None,
    attempts: tuple[dict[str, object], ...] = (),
) -> IdempotencyKeyResult:
    return IdempotencyKeyResult(
        outcome=OUTCOME_INSUFFICIENT_EVIDENCE,
        reason=reason,
        attempts=attempts,
        static_expectation=expectation,
        matches_static_expectation=None,
    )


def check_idempotency_key_stability(
    events: Iterable[Event],
    *,
    supported_path: str = DEFAULT_SUPPORTED_PATH,
    static_key_present: bool | None = None,
    static_key_reused: bool | None = None,
) -> IdempotencyKeyResult:
    """Compare key fingerprints across the attempts of one operation.

    Outcomes:

    - ``absent`` - no attempt sent a key.
    - ``stable`` - every attempt sent the same key fingerprint.
    - ``changing`` - key values (or key presence) differed between attempts.
    - ``insufficient_evidence`` - missing, malformed, or uncorrelatable
      observations. Never reported as a passing result.
    """

    trace = tuple(events)
    expectation = _static_expectation(static_key_present, static_key_reused)

    if not trace:
        return _insufficient(
            "event trace is empty; no key values were observed", expectation
        )

    collected = collect_operation_attempts(trace, supported_path=supported_path)
    if collected.error is not None:
        return _insufficient(collected.error, expectation)

    observations: list[dict[str, object]] = []
    fingerprints: list[str] = []
    for event in collected.counted:
        present = event.data.get("idempotency_key_present")
        fingerprint = event.data.get("idempotency_key_fingerprint")
        if present is True:
            if not _has_text(fingerprint):
                return _insufficient(
                    f"request_sent at seq {event.seq} is marked as keyed but "
                    "records no key fingerprint",
                    expectation,
                    tuple(observations),
                )
            fingerprints.append(str(fingerprint))
        elif present is not False:
            return _insufficient(
                f"request_sent at seq {event.seq} records no idempotency-key "
                "presence",
                expectation,
                tuple(observations),
            )
        observations.append(
            {
                "attempt_id": str(event.attempt_id),
                "present": present is True,
                "key_fingerprint": str(fingerprint) if present is True else None,
            }
        )

    total = len(observations)
    keyed = len(fingerprints)
    if keyed == 0:
        outcome = OUTCOME_ABSENT
        distinct = 0
        reason = f"none of {total} attempt(s) sent an Idempotency-Key"
    elif total < 2:
        # One keyed attempt can not establish stability: there is nothing to
        # compare it with, so this must not read as a passing result.
        return _insufficient(
            f"only {total} attempt was observed, so key stability can not be "
            "compared across attempts",
            expectation,
            tuple(observations),
        )
    elif keyed < total:
        outcome = OUTCOME_CHANGING
        distinct = len(set(fingerprints))
        reason = (
            f"the Idempotency-Key was present on {keyed} of {total} attempt(s); "
            "key presence changed across attempts"
        )
    else:
        distinct = len(set(fingerprints))
        if distinct == 1:
            outcome = OUTCOME_STABLE
            reason = f"all {total} attempt(s) sent the same Idempotency-Key"
        else:
            outcome = OUTCOME_CHANGING
            reason = (
                f"{distinct} distinct Idempotency-Key value(s) across "
                f"{total} attempt(s)"
            )

    matches = _matches_static(outcome, expectation)
    if matches is False:
        reason = f"{reason}; this contradicts the static expectation"
    return IdempotencyKeyResult(
        outcome=outcome,
        reason=reason,
        attempts=tuple(observations),
        distinct_key_count=distinct,
        static_expectation=expectation,
        matches_static_expectation=matches,
    )
