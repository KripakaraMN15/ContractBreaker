"""Evidence-integrity benchmark.

Each case stores an event trace plus the verdict a correct verifier must
produce. The suite calls the production verifiers directly - it never
re-implements verification - so a green integrity run is evidence about the
shipped verifiers rather than about a parallel test double.

Corrupt or contradictory traces must never be confirmed. A positive control is
included so the suite can not pass by returning ``inconclusive`` for everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import Event, EventKind
from .retryable_error_verifier import verify_retryable_error_duplicate
from .timeout_contract import DEFAULT_TIMEOUT_INVARIANT, TIMEOUT_INVARIANTS
from .timeout_verifier import verify_client_timeout
from .verifier import Report, Verdict, verify_duplicate_side_effects

_INTEGRITY_SCENARIOS = ("lost-response", "retryable-error", "client-timeout")
_VERDICTS = {verdict.value for verdict in Verdict}


class IntegritySuiteError(ValueError):
    """Raised when the integrity dataset is missing or malformed."""


@dataclass(frozen=True)
class IntegrityCaseResult:
    """One integrity case, its expected verdict, and what the verifier said."""

    case_id: str
    scenario: str
    expected_verdict: str
    actual_verdict: str
    passed: bool
    reason: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "scenario": self.scenario,
            "expected_verdict": self.expected_verdict,
            "actual_verdict": self.actual_verdict,
            "passed": self.passed,
            "reason": self.reason,
            "note": self.note,
        }


def integrity_cases_path(root: str | Path) -> Path:
    return Path(root) / "integrity" / "cases.json"


def _require_text(value: object, label: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegritySuiteError(f"{case_id}: {label} must be a non-empty string")
    return value


def _require_int(value: object, label: str, case_id: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IntegritySuiteError(f"{case_id}: {label} must be a non-negative integer")
    return value


def load_integrity_cases(root: str | Path) -> list[dict[str, Any]]:
    """Load and validate the stored integrity cases."""

    path = integrity_cases_path(root)
    if not path.is_file():
        raise IntegritySuiteError(f"integrity cases not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegritySuiteError(f"could not read integrity cases: {exc}") from exc

    raw_cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(raw_cases, list) or not raw_cases:
        raise IntegritySuiteError("integrity cases must be a non-empty list")

    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise IntegritySuiteError("integrity case must be an object")
        case_id = _require_text(raw.get("id"), "id", "<unknown>")
        if case_id in seen:
            raise IntegritySuiteError(f"duplicate integrity case id: {case_id}")
        seen.add(case_id)
        scenario = _require_text(raw.get("scenario"), "scenario", case_id)
        if scenario not in _INTEGRITY_SCENARIOS:
            raise IntegritySuiteError(
                f"{case_id}: unsupported integrity scenario {scenario!r}"
            )
        expected = _require_text(
            raw.get("expected_verdict"), "expected_verdict", case_id
        )
        if expected not in _VERDICTS:
            raise IntegritySuiteError(
                f"{case_id}: unknown expected verdict {expected!r}"
            )
        _require_int(raw.get("side_effects"), "side_effects", case_id)
        invariant = raw.get("invariant", DEFAULT_TIMEOUT_INVARIANT)
        if scenario == "client-timeout" and invariant not in TIMEOUT_INVARIANTS:
            raise IntegritySuiteError(
                f"{case_id}: unknown timeout invariant {invariant!r}"
            )
        events = raw.get("events")
        if not isinstance(events, list) or not events:
            raise IntegritySuiteError(
                f"{case_id}: events must be a non-empty list"
            )
        cases.append(raw)
    return cases


def _build_events(raw_events: list[Any], case_id: str) -> tuple[Event, ...]:
    events: list[Event] = []
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise IntegritySuiteError(
                f"{case_id}: event {index} must be an object"
            )
        try:
            kind = EventKind(raw.get("kind"))
        except ValueError as exc:
            raise IntegritySuiteError(
                f"{case_id}: event {index} has an unknown kind"
            ) from exc
        data = raw.get("data") or {}
        if not isinstance(data, dict):
            raise IntegritySuiteError(
                f"{case_id}: event {index} data must be an object"
            )
        events.append(
            Event(
                seq=raw.get("seq"),
                kind=kind,
                attempt_id=raw.get("attempt_id"),
                operation_id=raw.get("operation_id"),
                data=dict(data),
            )
        )
    return tuple(events)


def _verify(case: dict[str, Any], events: tuple[Event, ...]) -> Report:
    scenario = case["scenario"]
    if scenario == "lost-response":
        return verify_duplicate_side_effects(
            events, side_effects=case["side_effects"]
        )
    if scenario == "retryable-error":
        return verify_retryable_error_duplicate(
            events, side_effects=case["side_effects"]
        )
    return verify_client_timeout(
        events,
        side_effects=case["side_effects"],
        invariant_id=case.get("invariant", DEFAULT_TIMEOUT_INVARIANT),
        invariant_provenance="integrity_fixture",
    ).report


def run_integrity_suite(root: str | Path) -> dict[str, Any]:
    """Run every stored integrity case through the production verifiers."""

    results: list[IntegrityCaseResult] = []
    for case in load_integrity_cases(root):
        events = _build_events(case["events"], case["id"])
        report = _verify(case, events)
        actual = report.verdict.value
        results.append(
            IntegrityCaseResult(
                case_id=case["id"],
                scenario=case["scenario"],
                expected_verdict=case["expected_verdict"],
                actual_verdict=actual,
                passed=actual == case["expected_verdict"],
                reason=report.reason,
                note=str(case.get("note", "")),
            )
        )

    passed = sum(1 for result in results if result.passed)
    false_confirmed = sum(
        1
        for result in results
        if result.expected_verdict != "confirmed"
        and result.actual_verdict == "confirmed"
    )
    return {
        "suite": "evidence-integrity",
        "description": (
            "stored traces must produce the expected verdict; a corrupt or "
            "contradictory trace must never be confirmed"
        ),
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "false_confirmed": false_confirmed,
        "cases": [result.to_dict() for result in results],
    }
