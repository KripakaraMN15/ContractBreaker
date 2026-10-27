"""Verification for the client-timeout scenario.

The verifier answers two separate questions:

1. **Observation** - what happened? Did the client observe a timeout, and did
   the service commit a side effect after that observation?
2. **Violation** - does the declared invariant forbid that observation?

A late commit is only ``confirmed`` when the configured contract forbids it.
The timeout itself is exercised by the harness in the in-process WSGI
simulation; the trace must declare that provenance or the result is
``insufficient_evidence``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .events import Event, EventKind
from .timeout_contract import TIMEOUT_INVARIANTS
from .verifier import Report, Verdict

OBSERVATION_ORPHANED_COMMIT = "orphaned_commit"
OBSERVATION_NO_ORPHANED_COMMIT = "no_orphaned_commit"
OBSERVATION_SERVER_COMPLETED_FIRST = "server_completed_first"
OBSERVATION_INSUFFICIENT = "insufficient_evidence"

_TERMINAL_OUTCOMES = {"client_completed", "client_gave_up"}
_COMPLETION_OUTCOMES = {"committed", "aborted", "replayed"}


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


@dataclass(frozen=True)
class TimeoutResult:
    """The observed timeout behaviour plus the declared-contract judgement."""

    observation: str
    reason: str
    invariant_id: str
    invariant_statement: str
    invariant_provenance: str
    violated: bool | None
    report: Report
    client_timeout_observed: bool = False
    server_processing_completed: bool = False
    commit_after_timeout: bool = False
    commit_preceded_timeout: bool = False
    declared_timeout_seconds: float | None = None
    declared_timeout_source: str | None = None

    @property
    def verdict(self) -> Verdict:
        return self.report.verdict

    def to_dict(self) -> dict[str, Any]:
        """Serialize the timeout evidence for the report."""

        return {
            "observation": {
                "outcome": self.observation,
                "client_timeout_observed": self.client_timeout_observed,
                "server_processing_completed": self.server_processing_completed,
                "commit_after_timeout": self.commit_after_timeout,
                "commit_preceded_timeout": self.commit_preceded_timeout,
                "provenance": {
                    "outcome": "runtime_check",
                    "ordering": "event_sequence",
                },
                "reason": self.reason,
            },
            "invariant": {
                "id": self.invariant_id,
                "statement": self.invariant_statement,
                "provenance": self.invariant_provenance,
                "violated": self.violated,
            },
            "simulation": {
                "client_timeout_generated_by": (
                    "harness" if self.client_timeout_observed else None
                ),
                "timeout_enforced_by": "harness",
                "transport": "in_process_wsgi",
                "real_requests_timeout_enforced": False,
            },
            "declared_timeout_seconds": self.declared_timeout_seconds,
            "declared_timeout_provenance": self.declared_timeout_source,
        }


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


def verify_client_timeout(
    events: Iterable[Event],
    *,
    side_effects: int,
    invariant_id: str,
    invariant_provenance: str,
) -> TimeoutResult:
    """Verify one client-timeout experiment against a declared invariant."""

    trace = tuple(events)

    def result(
        *,
        observation: str,
        verdict: Verdict,
        reason: str,
        violated: bool | None,
        **evidence: Any,
    ) -> TimeoutResult:
        return TimeoutResult(
            observation=observation,
            reason=reason,
            invariant_id=invariant_id,
            invariant_statement=statement,
            invariant_provenance=invariant_provenance,
            violated=violated,
            report=Report(
                verdict=verdict,
                reason=reason,
                side_effects=side_effects,
                events=trace,
            ),
            **evidence,
        )

    statement = TIMEOUT_INVARIANTS.get(invariant_id)
    if statement is None:
        return TimeoutResult(
            observation=OBSERVATION_INSUFFICIENT,
            reason=f"unknown timeout invariant: {invariant_id!r}",
            invariant_id=invariant_id,
            invariant_statement="",
            invariant_provenance=invariant_provenance,
            violated=None,
            report=Report(
                verdict=Verdict.INCONCLUSIVE,
                reason=f"unknown timeout invariant: {invariant_id!r}",
                side_effects=side_effects,
                events=trace,
            ),
        )
    if isinstance(side_effects, bool) or not isinstance(side_effects, int):
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="side-effect count must be an integer",
            violated=None,
        )
    if side_effects < 0:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="side-effect count can not be negative",
            violated=None,
        )
    if not trace:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="event trace is empty",
            violated=None,
        )

    structure_error = _structure_error(trace)
    if structure_error is not None:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=structure_error,
            violated=None,
        )

    requests = [e for e in trace if e.kind is EventKind.REQUEST_SENT]
    received = [e for e in trace if e.kind is EventKind.REQUEST_RECEIVED]
    commits = [e for e in trace if e.kind is EventKind.SIDE_EFFECT_COMMITTED]
    returns = [e for e in trace if e.kind is EventKind.RESPONSE_RETURNED]
    drops = [e for e in trace if e.kind is EventKind.RESPONSE_DROPPED]
    timeouts = [e for e in trace if e.kind is EventKind.CLIENT_TIMEOUT_OBSERVED]
    completions = [
        e for e in trace if e.kind is EventKind.SERVER_PROCESSING_COMPLETED
    ]

    if drops:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the client-timeout scenario does not model a dropped response",
            violated=None,
        )
    if len(requests) != 1:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "the client-timeout scenario models exactly one attempt; "
                f"the trace contains {len(requests)}"
            ),
            violated=None,
        )
    request = requests[0]
    attempt_id = request.attempt_id
    operation_id = request.operation_id
    if not _has_text(attempt_id) or not _has_text(operation_id):
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the request attempt or operation identifier is missing",
            violated=None,
        )

    def correlated(event: Event, label: str) -> str | None:
        if event.attempt_id != attempt_id:
            return f"{label} does not belong to the observed attempt"
        if event.operation_id != operation_id:
            return f"{label} does not belong to the observed operation"
        return None

    for event in [*received, *commits, *returns, *timeouts, *completions]:
        problem = correlated(event, event.kind.value)
        if problem is not None:
            return result(
                observation=OBSERVATION_INSUFFICIENT,
                verdict=Verdict.INCONCLUSIVE,
                reason=problem,
                violated=None,
            )

    if len(received) > 1:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the attempt was received more than once",
            violated=None,
        )
    if len(commits) > 1:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the attempt committed more than one side effect",
            violated=None,
        )
    if len(completions) != 1:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "trace must contain exactly one server_processing_completed "
                "event"
            ),
            violated=None,
        )
    completion = completions[0]
    completion_outcome = completion.data.get("outcome")
    if completion_outcome not in _COMPLETION_OUTCOMES:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "server_processing_completed has an unknown outcome: "
                f"{completion_outcome!r}"
            ),
            violated=None,
        )
    if completion.data.get("origin") != "test_service":
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="server completion does not declare test-service provenance",
            violated=None,
        )
    if side_effects != len(commits):
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                f"observed {side_effects} side effects but the trace records "
                f"{len(commits)} commits"
            ),
            violated=None,
        )

    declared = request.data.get("declared_timeout_seconds")
    declared_source = request.data.get("declared_timeout_source")

    if not timeouts:
        # Control run: the server finished and the client got a response.
        if len(returns) != 1:
            return result(
                observation=OBSERVATION_INSUFFICIENT,
                verdict=Verdict.INCONCLUSIVE,
                reason=(
                    "no client timeout was observed and no single response was "
                    "returned"
                ),
                violated=None,
            )
        if completion_outcome != "committed" or not commits:
            return result(
                observation=OBSERVATION_INSUFFICIENT,
                verdict=Verdict.INCONCLUSIVE,
                reason=(
                    "a response was returned without a committed side effect"
                ),
                violated=None,
            )
        if completion.seq <= request.seq:
            return result(
                observation=OBSERVATION_INSUFFICIENT,
                verdict=Verdict.INCONCLUSIVE,
                reason="server completion precedes the request",
                violated=None,
            )
        return result(
            observation=OBSERVATION_SERVER_COMPLETED_FIRST,
            verdict=Verdict.NOT_REPRODUCED,
            reason=(
                "the server completed and returned a response before any "
                "client timeout was observed"
            ),
            violated=False,
            server_processing_completed=True,
            declared_timeout_seconds=declared if _is_number(declared) else None,
            declared_timeout_source=declared_source,
        )

    if len(timeouts) != 1:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="trace contains more than one client timeout",
            violated=None,
        )
    timeout = timeouts[0]

    if timeout.data.get("enforced_by") != "harness":
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="timeout evidence does not declare harness provenance",
            violated=None,
        )
    if timeout.data.get("origin") != "harness_timeout_fault":
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="timeout evidence does not declare harness origin",
            violated=None,
        )
    if timeout.data.get("real_requests_timeout_enforced") is not False:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "timeout evidence claims the real requests timeout was "
                "enforced, which this harness can not verify"
            ),
            violated=None,
        )
    timeout_declared = timeout.data.get("declared_timeout_seconds")
    if not _is_number(timeout_declared):
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="timeout evidence does not record a positive declared timeout",
            violated=None,
        )
    if _is_number(declared) and float(declared) != float(timeout_declared):
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="declared timeout disagrees with the request-timeout context",
            violated=None,
        )
    if returns:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "a response was returned for an attempt that also observed a "
                "timeout"
            ),
            violated=None,
        )
    if timeout.seq <= request.seq:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the client timeout precedes or coincides with its request",
            violated=None,
        )
    if received and received[0].seq >= timeout.seq:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the client timeout precedes the server receiving the request",
            violated=None,
        )
    if not received:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the server never observed the request",
            violated=None,
        )
    if completion.seq <= timeout.seq:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="server completion does not follow the client timeout",
            violated=None,
        )

    commit = commits[0] if commits else None
    commit_after = commit is not None and commit.seq > timeout.seq
    commit_before = commit is not None and commit.seq < timeout.seq

    if commit_after:
        if completion_outcome != "committed":
            return result(
                observation=OBSERVATION_INSUFFICIENT,
                verdict=Verdict.INCONCLUSIVE,
                reason=(
                    "a commit after the timeout contradicts the recorded "
                    "server completion outcome"
                ),
                violated=None,
            )
        observation = OBSERVATION_ORPHANED_COMMIT
        if invariant_id == "abort-on-timeout":
            violated: bool | None = True
            verdict = Verdict.CONFIRMED
            reason = (
                "the client observed a timeout and the server still committed a "
                "side effect afterwards; this violates the declared invariant "
                f"{invariant_id} ({statement})"
            )
        else:
            violated = False
            verdict = Verdict.NOT_REPRODUCED
            reason = (
                "an orphaned side effect was observed, but the declared "
                f"invariant {invariant_id} permits it; no violation was "
                "established"
            )
        return result(
            observation=observation,
            verdict=verdict,
            reason=reason,
            violated=violated,
            client_timeout_observed=True,
            server_processing_completed=True,
            commit_after_timeout=True,
            declared_timeout_seconds=float(timeout_declared),
            declared_timeout_source=timeout.data.get("declared_timeout_source"),
        )

    if commit_before and completion_outcome != "committed":
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                "a commit before the timeout contradicts the recorded server "
                "completion outcome"
            ),
            violated=None,
        )
    if completion_outcome == "committed" and commit is None:
        return result(
            observation=OBSERVATION_INSUFFICIENT,
            verdict=Verdict.INCONCLUSIVE,
            reason="the server reports a commit that the trace does not contain",
            violated=None,
        )

    return result(
        observation=OBSERVATION_NO_ORPHANED_COMMIT,
        verdict=Verdict.NOT_REPRODUCED,
        reason=(
            "the client observed a timeout but no side effect was committed "
            "after it; the declared invariant was not violated"
        ),
        violated=False,
        client_timeout_observed=True,
        server_processing_completed=True,
        commit_preceded_timeout=commit_before,
        declared_timeout_seconds=float(timeout_declared),
        declared_timeout_source=timeout.data.get("declared_timeout_source"),
    )
