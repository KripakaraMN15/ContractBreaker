"""Milestone 9 tests for retry-budget verification and evidence provenance."""

from __future__ import annotations

from pathlib import Path

import flask
import pytest
import requests

from contractbreaker import (
    OUTCOME_BUDGET_EXCEEDED,
    OUTCOME_INSUFFICIENT_EVIDENCE,
    OUTCOME_WITHIN_BUDGET,
    Event,
    EventKind,
    EventLog,
    FaultInjectionAdapter,
    FaultPlan,
    check_retry_budget,
)
from contractbreaker.pipeline import AnalysisOptions, analyze

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = REPO_ROOT / "benchmark" / "development"

OVER_BUDGET_SOURCE = '''import requests

for outer in range(2):
    for inner in range(2):
        try:
            requests.post(
                "http://service.test/payments",
                json={"amount_cents": 1999},
                timeout=5,
            )
        except requests.exceptions.Timeout:
            continue
'''

RETRYABLE_BUGGY = '''import requests

for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        json={"amount_cents": 1999},
        timeout=5,
    )
    if response.status_code == 503:
        continue
    response.raise_for_status()
    break
'''


def build_trace(
    *,
    max_attempts: int | None = 2,
    max_retries: int | None = None,
    attempts: tuple[str | None, ...] = ("attempt-1", "attempt-2"),
    paths: tuple[str | None, ...] | None = None,
    operation_ids: tuple[str | None, ...] | None = None,
    include_policy: int = 1,
) -> list[Event]:
    """Build a minimal budget trace without going through the runtime."""

    if max_retries is None and max_attempts is not None:
        max_retries = max_attempts - 1
    policy_data: dict[str, object] = {}
    if max_attempts is not None:
        policy_data["max_attempts"] = max_attempts
    if max_retries is not None:
        policy_data["max_retries"] = max_retries

    events: list[Event] = []
    seq = 1
    for _ in range(include_policy):
        events.append(
            Event(
                seq=seq,
                kind=EventKind.RETRY_POLICY_CONFIGURED,
                data=dict(policy_data),
            )
        )
        seq += 1

    resolved_paths = paths or tuple("/payments" for _ in attempts)
    resolved_operations = operation_ids or tuple("op-1" for _ in attempts)
    for attempt_id, path, operation_id in zip(
        attempts, resolved_paths, resolved_operations
    ):
        data: dict[str, object] = {}
        if path is not None:
            data["path"] = path
        events.append(
            Event(
                seq=seq,
                kind=EventKind.REQUEST_SENT,
                attempt_id=attempt_id,
                operation_id=operation_id,
                data=data,
            )
        )
        seq += 1
    return events


def analyze_file(path: Path, scenario: str = "lost-response") -> dict:
    return analyze(AnalysisOptions(source=path, scenario=scenario))


# --- budget outcomes -------------------------------------------------------


def test_no_retry_permitted_is_within_budget() -> None:
    result = check_retry_budget(build_trace(max_attempts=1, attempts=("attempt-1",)))

    assert result.outcome == OUTCOME_WITHIN_BUDGET
    assert result.expected_max_attempts == 1
    assert result.observed_attempts == 1
    assert result.counted_attempt_ids == ("attempt-1",)


def test_exactly_permitted_attempts_is_within_budget() -> None:
    result = check_retry_budget(build_trace(max_attempts=2))

    assert result.outcome == OUTCOME_WITHIN_BUDGET
    assert (result.expected_max_attempts, result.observed_attempts) == (2, 2)


def test_more_attempts_than_permitted_is_budget_exceeded() -> None:
    result = check_retry_budget(
        build_trace(
            max_attempts=2,
            attempts=("attempt-1", "attempt-2", "attempt-3"),
        )
    )

    assert result.outcome == OUTCOME_BUDGET_EXCEEDED
    assert result.exceeded is True
    assert (result.expected_max_attempts, result.observed_attempts) == (2, 3)
    assert "exceeds the declared maximum of 2" in result.reason


def test_retry_count_alone_can_declare_the_budget() -> None:
    result = check_retry_budget(
        build_trace(max_attempts=None, max_retries=0, attempts=("attempt-1",))
    )

    assert result.outcome == OUTCOME_WITHIN_BUDGET
    assert result.expected_max_attempts == 1
    assert result.expected_source == "max_retries_plus_one"


# --- unrelated requests ----------------------------------------------------


def test_unrelated_requests_are_not_counted() -> None:
    result = check_retry_budget(
        build_trace(
            attempts=("attempt-1", "unrelated-1", "attempt-2"),
            paths=("/payments", "/ping", "/payments"),
        )
    )

    assert result.outcome == OUTCOME_WITHIN_BUDGET
    assert result.observed_attempts == 2
    assert result.ignored_attempt_ids == ("unrelated-1",)
    assert "1 unrelated request(s) excluded" in result.reason


def test_unrelated_requests_cannot_mask_an_exceeded_budget() -> None:
    result = check_retry_budget(
        build_trace(
            attempts=("attempt-1", "attempt-2", "attempt-3", "unrelated-1"),
            paths=("/payments", "/payments", "/payments", "/ping"),
        )
    )

    assert result.outcome == OUTCOME_BUDGET_EXCEEDED
    assert result.observed_attempts == 3


def test_only_unrelated_requests_is_insufficient() -> None:
    result = check_retry_budget(
        build_trace(attempts=("unrelated-1",), paths=("/ping",))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert result.observed_attempts is None


# --- insufficient evidence -------------------------------------------------


def test_empty_trace_is_insufficient() -> None:
    result = check_retry_budget([])

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "empty" in result.reason


def test_missing_policy_is_insufficient() -> None:
    result = check_retry_budget(build_trace(include_policy=0))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "exactly one retry budget declaration" in result.reason


def test_two_policies_are_insufficient() -> None:
    result = check_retry_budget(build_trace(include_policy=2))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE


def test_invalid_policy_is_insufficient() -> None:
    result = check_retry_budget(build_trace(max_attempts=0, max_retries=0))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE


def test_contradictory_policy_is_insufficient() -> None:
    result = check_retry_budget(build_trace(max_attempts=3, max_retries=1))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "self-consistent" in result.reason


def test_no_requests_is_insufficient() -> None:
    result = check_retry_budget(build_trace(attempts=()))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "no request_sent" in result.reason


@pytest.mark.parametrize("attempt_id", [None, "", "   "])
def test_missing_attempt_identifier_is_insufficient(attempt_id) -> None:
    result = check_retry_budget(build_trace(attempts=(attempt_id, "attempt-2")))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "attempt identifier" in result.reason


def test_duplicate_attempt_identifier_is_insufficient() -> None:
    result = check_retry_budget(
        build_trace(attempts=("attempt-1", "attempt-1"))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "more than one request" in result.reason


def test_missing_operation_identifier_is_insufficient() -> None:
    result = check_retry_budget(build_trace(operation_ids=(None, "op-1")))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "operation identifier" in result.reason


def test_attempts_from_two_operations_are_insufficient() -> None:
    result = check_retry_budget(
        build_trace(operation_ids=("op-1", "op-2"))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "more than one logical operation" in result.reason


def test_attempt_without_a_path_is_insufficient() -> None:
    result = check_retry_budget(build_trace(paths=(None, "/payments")))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "records no path" in result.reason


def test_missing_evidence_never_reports_a_pass() -> None:
    for trace in (
        [],
        build_trace(include_policy=0),
        build_trace(attempts=(None,)),
        build_trace(attempts=("attempt-1", "attempt-1")),
    ):
        assert check_retry_budget(trace).outcome != OUTCOME_WITHIN_BUDGET


# --- provenance ------------------------------------------------------------


def test_provenance_separates_expectation_from_observation() -> None:
    payload = check_retry_budget(build_trace()).to_dict()

    assert payload["provenance"]["expected_max_attempts"] == (
        "derived_from_extraction"
    )
    assert payload["provenance"]["observed_attempts"] == "runtime_observation"
    assert payload["provenance"]["outcome"] == "runtime_check"


def test_insufficient_evidence_marks_unresolved_fields_not_evaluated() -> None:
    payload = check_retry_budget(build_trace(include_policy=0)).to_dict()

    assert payload["expected_max_attempts"] is None
    assert payload["observed_attempts"] is None
    assert payload["provenance"]["expected_max_attempts"] == "not_evaluated"
    assert payload["provenance"]["observed_attempts"] == "not_evaluated"


# --- fault targeting -------------------------------------------------------


def _two_route_app() -> flask.Flask:
    app = flask.Flask(__name__)

    @app.post("/payments")
    def payments():
        return {"ok": True}, 201

    @app.get("/ping")
    def ping():
        return {"ok": True}, 200

    return app


def test_fault_targets_payment_attempt_not_unrelated_request() -> None:
    log = EventLog()
    adapter = FaultInjectionAdapter(
        _two_route_app(),
        log,
        FaultPlan(drop_response_on_attempts=frozenset({1})),
    )
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.get("http://service.test/ping", timeout=1)
    with pytest.raises(requests.exceptions.ReadTimeout):
        session.post(
            "http://service.test/payments", json={"amount_cents": 1}, timeout=1
        )

    sent = [e for e in log.events if e.kind is EventKind.REQUEST_SENT]
    assert [e.attempt_id for e in sent] == ["unrelated-1", "attempt-1"]
    drops = [e for e in log.events if e.kind is EventKind.RESPONSE_DROPPED]
    assert [e.attempt_id for e in drops] == ["attempt-1"]


def test_unrelated_request_does_not_shift_the_drop_target() -> None:
    log = EventLog()
    adapter = FaultInjectionAdapter(
        _two_route_app(),
        log,
        FaultPlan(drop_response_on_attempts=frozenset({2})),
    )
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.get("http://service.test/ping", timeout=1)
    first = session.post(
        "http://service.test/payments", json={"amount_cents": 1}, timeout=1
    )
    assert first.status_code == 201
    # The second *payment* attempt loses its response. Without path-scoped
    # numbering the unrelated GET would have occupied attempt-1 and the drop
    # would have landed on the first payment request instead.
    with pytest.raises(requests.exceptions.ReadTimeout):
        session.post(
            "http://service.test/payments", json={"amount_cents": 1}, timeout=1
        )

    sent = [e for e in log.events if e.kind is EventKind.REQUEST_SENT]
    assert [e.attempt_id for e in sent] == [
        "unrelated-1",
        "attempt-1",
        "attempt-2",
    ]
    drops = [e for e in log.events if e.kind is EventKind.RESPONSE_DROPPED]
    assert [e.attempt_id for e in drops] == ["attempt-2"]


# --- end-to-end ------------------------------------------------------------


def test_buggy_fixture_confirms_duplicate_within_budget() -> None:
    report = analyze_file(DEVELOPMENT / "dev_lost_for_buggy.py")

    assert report["verdict"] == "confirmed"
    budget = report["execution"]["retry_budget"]
    assert budget["outcome"] == OUTCOME_WITHIN_BUDGET
    assert budget["expected_max_attempts"] == 2
    assert budget["observed_attempts"] == 2


def test_fixed_fixture_is_not_reproduced_within_budget() -> None:
    report = analyze_file(DEVELOPMENT / "dev_lost_for_fixed.py")

    assert report["verdict"] == "not_reproduced"
    budget = report["execution"]["retry_budget"]
    assert budget["outcome"] == OUTCOME_WITHIN_BUDGET
    assert budget["observed_attempts"] == 2


def test_over_budget_client_is_confirmed_and_separately_flagged(tmp_path) -> None:
    source = tmp_path / "over_budget.py"
    source.write_text(OVER_BUDGET_SOURCE, encoding="utf-8")

    report = analyze_file(source)

    assert report["verdict"] == "confirmed"
    budget = report["execution"]["retry_budget"]
    assert budget["outcome"] == OUTCOME_BUDGET_EXCEEDED
    assert budget["expected_max_attempts"] == 2
    assert budget["observed_attempts"] == 4
    assert report["execution"]["observed_side_effects"] == 4


def test_blocked_client_has_no_budget_evidence(tmp_path) -> None:
    source = tmp_path / "unknown.py"
    source.write_text(
        "import requests\n"
        "for attempt in range(max_retries + 1):\n"
        "    try:\n"
        "        requests.post('http://service.test/payments', timeout=5)\n"
        "    except requests.exceptions.Timeout:\n"
        "        continue\n",
        encoding="utf-8",
    )

    report = analyze_file(source)

    assert report["verdict"] == "inconclusive"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["retry_budget"] is None


def test_harness_retry_decision_is_attributed_to_the_harness(tmp_path) -> None:
    source = tmp_path / "retryable.py"
    source.write_text(RETRYABLE_BUGGY, encoding="utf-8")

    report = analyze_file(source, scenario="retryable-error")

    assert report["verdict"] == "confirmed"
    decisions = [
        event
        for event in report["execution"]["events"]
        if event["kind"] == "retry_scheduled"
    ]
    assert len(decisions) == 1
    assert decisions[0]["data"]["decision_provenance"] == "harness_configured"
    assert "recorded by the harness, not the client" in report["verdict_reason"]
    assert any(
        "harness_configured" in limitation
        for limitation in report["limitations"]
    )
