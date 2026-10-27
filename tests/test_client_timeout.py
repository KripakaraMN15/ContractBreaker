"""Milestone 11 tests: client-timeout experiment, evidence, and invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from contractbreaker import (
    Event,
    EventKind,
    verify_client_timeout,
)
from contractbreaker.pipeline import AnalysisOptions, analyze
from contractbreaker.report import render_text
from contractbreaker.runtime import RuntimeExecutionTimeout
from contractbreaker.timeout_verifier import (
    OBSERVATION_INSUFFICIENT,
    OBSERVATION_NO_ORPHANED_COMMIT,
    OBSERVATION_ORPHANED_COMMIT,
    OBSERVATION_SERVER_COMPLETED_FIRST,
)

CLIENT_TIMEOUT_SOURCE = """import requests

response = requests.post(
    "http://service.test/payments",
    json={"amount_cents": 1999},
    timeout=5,
)
print(response.status_code)
"""


def ev(
    seq: int,
    kind: EventKind,
    attempt: str | None = "attempt-1",
    operation: str | None = "op-1",
    **data: object,
) -> Event:
    return Event(
        seq=seq,
        kind=kind,
        attempt_id=attempt,
        operation_id=operation,
        data=dict(data),
    )


def request_event(seq: int = 1, **overrides: object) -> Event:
    data = {
        "path": "/payments",
        "declared_timeout_seconds": 5.0,
        "declared_timeout_source": "derived_from_extraction",
        "idempotency_key_present": False,
        "idempotency_key_fingerprint": None,
    }
    data.update(overrides)
    return ev(seq, EventKind.REQUEST_SENT, **data)


def timeout_event(seq: int = 3, **overrides: object) -> Event:
    data = {
        "declared_timeout_seconds": 5.0,
        "declared_timeout_source": "derived_from_extraction",
        "enforced_by": "harness",
        "timeout_transport": "in_process_wsgi",
        "origin": "harness_timeout_fault",
        "real_requests_timeout_enforced": False,
        "timeout_mode": "client_first_then_commit",
    }
    data.update(overrides)
    return ev(seq, EventKind.CLIENT_TIMEOUT_OBSERVED, **data)


def completion_event(seq: int = 5, **overrides: object) -> Event:
    data = {"outcome": "committed", "origin": "test_service"}
    data.update(overrides)
    return ev(seq, EventKind.SERVER_PROCESSING_COMPLETED, **data)


def orphan_trace(**timeout_overrides: object) -> list[Event]:
    return [
        request_event(),
        ev(2, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        timeout_event(**timeout_overrides),
        ev(4, EventKind.SIDE_EFFECT_COMMITTED, side_effect_id="effect-1"),
        completion_event(),
        ev(6, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_gave_up"),
    ]


def verify(trace, *, side_effects=1, invariant="abort-on-timeout"):
    return verify_client_timeout(
        trace,
        side_effects=side_effects,
        invariant_id=invariant,
        invariant_provenance="cli",
    )


# --- observation -----------------------------------------------------------


def test_commit_after_timeout_is_an_orphaned_side_effect() -> None:
    result = verify(orphan_trace())

    assert result.observation == OBSERVATION_ORPHANED_COMMIT
    assert result.commit_after_timeout is True
    assert result.client_timeout_observed is True
    assert result.server_processing_completed is True
    assert result.report.side_effects == 1


def test_timeout_with_no_commit_has_no_orphan() -> None:
    trace = [
        request_event(),
        ev(2, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        timeout_event(),
        completion_event(4, outcome="aborted"),
        ev(5, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_gave_up"),
    ]

    result = verify(trace, side_effects=0)

    assert result.observation == OBSERVATION_NO_ORPHANED_COMMIT
    assert result.commit_after_timeout is False


def test_server_completed_first_is_not_a_timeout() -> None:
    trace = [
        request_event(),
        ev(2, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        ev(3, EventKind.SIDE_EFFECT_COMMITTED, side_effect_id="effect-1"),
        completion_event(4),
        ev(5, EventKind.RESPONSE_RETURNED, status_code=201),
        ev(6, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_completed"),
    ]

    result = verify(trace)

    assert result.observation == OBSERVATION_SERVER_COMPLETED_FIRST
    assert result.client_timeout_observed is False


def test_commit_before_timeout_is_recorded_but_not_orphaned() -> None:
    trace = [
        request_event(),
        ev(2, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        ev(3, EventKind.SIDE_EFFECT_COMMITTED, side_effect_id="effect-1"),
        timeout_event(4),
        completion_event(5),
        ev(6, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_gave_up"),
    ]

    result = verify(trace)

    assert result.observation == OBSERVATION_NO_ORPHANED_COMMIT
    assert result.commit_preceded_timeout is True
    assert result.commit_after_timeout is False


# --- invariant versus observation ------------------------------------------


def test_same_observation_is_confirmed_only_when_the_invariant_forbids_it() -> None:
    trace = orphan_trace()

    forbidden = verify(trace, invariant="abort-on-timeout")
    allowed = verify(trace, invariant="allow-orphaned-commit")

    assert forbidden.observation == allowed.observation == OBSERVATION_ORPHANED_COMMIT
    assert forbidden.violated is True
    assert forbidden.verdict.value == "confirmed"
    assert allowed.violated is False
    assert allowed.verdict.value == "not_reproduced"
    assert "permits it" in allowed.report.reason


def test_invariant_statement_and_provenance_are_reported() -> None:
    payload = verify(orphan_trace()).to_dict()

    assert payload["invariant"]["id"] == "abort-on-timeout"
    assert payload["invariant"]["provenance"] == "cli"
    assert payload["invariant"]["violated"] is True
    assert "must not be committed" in payload["invariant"]["statement"]


def test_unknown_invariant_is_insufficient_evidence() -> None:
    result = verify(orphan_trace(), invariant="not-a-real-invariant")

    assert result.observation == OBSERVATION_INSUFFICIENT
    assert result.violated is None
    assert result.verdict.value == "inconclusive"


# --- simulated-timeout provenance ------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"enforced_by": "requests"},
        {"origin": "client_socket"},
        {"real_requests_timeout_enforced": True},
        {"declared_timeout_seconds": None},
    ],
)
def test_timeout_evidence_must_declare_harness_provenance(overrides) -> None:
    result = verify(orphan_trace(**overrides))

    assert result.observation == OBSERVATION_INSUFFICIENT
    assert result.verdict.value == "inconclusive"


def test_declared_timeout_must_match_the_request_context() -> None:
    trace = orphan_trace()
    trace[0] = request_event(declared_timeout_seconds=3.0)

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


def test_report_states_the_simulation_never_enforces_real_timeouts() -> None:
    payload = verify(orphan_trace()).to_dict()

    assert payload["simulation"] == {
        "client_timeout_generated_by": "harness",
        "timeout_enforced_by": "harness",
        "transport": "in_process_wsgi",
        "real_requests_timeout_enforced": False,
    }


# --- insufficient evidence -------------------------------------------------


def test_missing_server_completion_is_insufficient() -> None:
    trace = [e for e in orphan_trace() if e.kind is not EventKind.SERVER_PROCESSING_COMPLETED]

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT
    assert "exactly one server_processing_completed" in result.reason


def test_missing_timeout_and_response_is_insufficient() -> None:
    trace = [
        request_event(),
        ev(2, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        ev(3, EventKind.SIDE_EFFECT_COMMITTED, side_effect_id="effect-1"),
        completion_event(4),
        ev(5, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_completed"),
    ]

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


def test_two_attempts_are_insufficient() -> None:
    trace = [
        request_event(1),
        request_event(2),
        ev(3, EventKind.REQUEST_RECEIVED, has_idempotency_key=False),
        timeout_event(4),
        ev(5, EventKind.SIDE_EFFECT_COMMITTED, side_effect_id="effect-1"),
        completion_event(6),
        ev(7, EventKind.SCENARIO_END, attempt=None, operation=None, outcome="client_gave_up"),
    ]

    result = verify(trace, side_effects=1)

    assert result.observation == OBSERVATION_INSUFFICIENT
    assert "exactly one attempt" in result.reason


def test_mismatched_operation_identifier_is_insufficient() -> None:
    trace = orphan_trace()
    trace[3] = ev(
        4,
        EventKind.SIDE_EFFECT_COMMITTED,
        operation="op-other",
        side_effect_id="effect-1",
    )

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


def test_ledger_disagreement_is_insufficient() -> None:
    result = verify(orphan_trace(), side_effects=2)

    assert result.observation == OBSERVATION_INSUFFICIENT
    assert "side effects" in result.reason


def test_duplicate_sequence_numbers_are_insufficient() -> None:
    trace = orphan_trace()
    trace[2] = timeout_event(2)

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


def test_completion_outcome_contradicting_the_commit_is_insufficient() -> None:
    trace = orphan_trace()
    trace[4] = completion_event(outcome="aborted")

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


def test_dropped_response_is_not_part_of_this_scenario() -> None:
    trace = orphan_trace()
    trace.insert(3, ev(3, EventKind.RESPONSE_DROPPED, status_code=201))

    result = verify(trace)

    assert result.observation == OBSERVATION_INSUFFICIENT


# --- end to end ------------------------------------------------------------


def write_client(tmp_path: Path, source: str = CLIENT_TIMEOUT_SOURCE) -> Path:
    path = tmp_path / "client.py"
    path.write_text(source, encoding="utf-8")
    return path


def run_timeout(tmp_path: Path, **options) -> dict:
    return analyze(
        AnalysisOptions(
            source=write_client(tmp_path),
            scenario="client-timeout",
            **options,
        )
    )


def test_orphaned_commit_is_confirmed_under_the_default_invariant(tmp_path) -> None:
    report = run_timeout(tmp_path)

    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 1
    timeout = report["execution"]["timeout"]
    assert timeout["observation"]["outcome"] == OBSERVATION_ORPHANED_COMMIT
    assert timeout["invariant"]["id"] == "abort-on-timeout"
    assert timeout["invariant"]["provenance"] == "scenario_default"
    assert timeout["invariant"]["violated"] is True
    settings = report["scenario_configuration"]["settings"]
    assert settings["timeout_mode"] == "client_first_then_commit"
    assert settings["timeout_invariant"] == "abort-on-timeout"


def test_allowed_orphaned_commit_is_not_a_confirmed_bug(tmp_path) -> None:
    report = run_timeout(tmp_path, timeout_invariant="allow-orphaned-commit")

    assert report["verdict"] == "not_reproduced"
    timeout = report["execution"]["timeout"]
    assert timeout["observation"]["outcome"] == OBSERVATION_ORPHANED_COMMIT
    assert timeout["invariant"]["violated"] is False


def test_aborted_server_work_is_not_reproduced(tmp_path) -> None:
    report = run_timeout(tmp_path, timeout_mode="client_first_no_commit")

    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 0
    assert report["execution"]["timeout"]["observation"]["outcome"] == (
        OBSERVATION_NO_ORPHANED_COMMIT
    )


def test_server_first_is_not_reproduced(tmp_path) -> None:
    report = run_timeout(tmp_path, timeout_mode="server_first")

    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    assert report["execution"]["timeout"]["observation"]["outcome"] == (
        OBSERVATION_SERVER_COMPLETED_FIRST
    )


def test_report_discloses_the_harness_timeout_and_limitations(tmp_path) -> None:
    report = run_timeout(tmp_path)

    simulation = report["execution"]["timeout"]["simulation"]
    assert simulation["real_requests_timeout_enforced"] is False
    assert simulation["timeout_enforced_by"] == "harness"
    assert any(
        "injected by the harness in the in-process WSGI simulation" in text
        for text in report["limitations"]
    )
    assert any(
        "requests timeout parameter is not enforced" in text
        for text in report["limitations"]
    )
    rendered = render_text(report)
    assert "real_requests_timeout_enforced=False" in rendered
    assert "client timeout: orphaned_commit" in rendered


def test_scenario_configuration_records_the_invariant(tmp_path) -> None:
    report = run_timeout(tmp_path, timeout_invariant="allow-orphaned-commit")
    configuration = report["scenario_configuration"]

    assert configuration["settings"]["timeout_invariant"] == (
        "allow-orphaned-commit"
    )
    assert configuration["provenance"]["timeout_invariant"] == "cli"
    assert configuration["provenance"]["timeout_mode"] == "scenario_default"


def test_harness_execution_timeout_is_structurally_separate(
    tmp_path, monkeypatch
) -> None:
    def timeout(**kwargs):
        raise RuntimeExecutionTimeout("runtime execution timed out after 10.0s")

    monkeypatch.setattr("contractbreaker.pipeline.run_analyzed_client", timeout)

    report = run_timeout(tmp_path)

    assert report["analysis_status"] == "not_runnable"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["timeout"] is None
    assert report["execution"]["failure"] == {
        "kind": "harness_timeout",
        "message": "runtime execution timed out after 10.0s",
    }
    assert report["verdict"] == "inconclusive"


def test_worker_error_is_reported_with_a_different_kind(tmp_path, monkeypatch) -> None:
    from contractbreaker.runtime import RuntimeExecutionError

    def fail(**kwargs):
        raise RuntimeExecutionError("runtime worker produced no result")

    monkeypatch.setattr("contractbreaker.pipeline.run_analyzed_client", fail)

    report = run_timeout(tmp_path)

    assert report["execution"]["failure"]["kind"] == "worker_error"
    assert report["execution"]["timeout"] is None


def test_unsupported_source_is_blocked(tmp_path) -> None:
    path = tmp_path / "client.py"
    path.write_text(
        "import httpx\nhttpx.post('http://service.test/payments', timeout=1)\n",
        encoding="utf-8",
    )

    report = analyze(
        AnalysisOptions(source=path, scenario="client-timeout")
    )

    assert report["verdict"] == "inconclusive"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["timeout"] is None
    assert report["hypothesis"]["scenario_readiness"]["runnable"] is False


def test_timeout_flags_are_rejected_for_other_scenarios(tmp_path, capsys) -> None:
    from contractbreaker.cli import main

    path = write_client(tmp_path)
    code = main(
        [
            "analyze",
            str(path),
            "--scenario",
            "lost-response",
            "--timeout-invariant",
            "abort-on-timeout",
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    assert "only valid with --scenario client-timeout" in captured.err
