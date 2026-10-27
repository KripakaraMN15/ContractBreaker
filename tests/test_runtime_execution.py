"""Runtime execution tests for the analyzed client contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from contractbreaker.pipeline import AnalysisOptions, analyze
from contractbreaker.report import (
    EXIT_CONFIRMED,
    EXIT_INCONCLUSIVE,
    EXIT_NOT_REPRODUCED,
    exit_code_for_report,
)
from contractbreaker.runtime import RuntimeExecutionError, run_analyzed_client

LOST_BUGGY = """import requests

for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

LOST_FIXED = """import uuid
import requests

idempotency_key = str(uuid.uuid4())
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

NO_RETRY = """import requests

for attempt in range(2):
    try:
        response = requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        continue
"""

NESTED_CLIENT = """import requests

def send():
    for attempt in range(2):
        try:
            requests.post("http://service.test/payments", timeout=5)
        except requests.exceptions.Timeout:
            continue
"""

INCOMPLETE_TRACE = """import requests

for attempt in range(2):
    try:
        raise RuntimeError("stop before request")
        requests.post("http://service.test/payments", timeout=5)
    except requests.exceptions.Timeout:
        continue
"""

RETRYABLE_BUGGY = """import requests

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
"""

RETRYABLE_FIXED = """import uuid
import requests

idempotency_key = str(uuid.uuid4())
for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        headers={"Idempotency-Key": idempotency_key},
        timeout=5,
    )
    if response.status_code == 503:
        continue
    response.raise_for_status()
    break
"""


def write_source(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def run_analysis(
    tmp_path: Path,
    name: str,
    source: str,
    *,
    scenario: str = "lost-response",
) -> tuple[dict, int]:
    path = write_source(tmp_path, name, source)
    report = analyze(AnalysisOptions(source=path, scenario=scenario))
    return report, exit_code_for_report(report)


def test_supported_buggy_lost_response_client_is_confirmed(tmp_path) -> None:
    report, code = run_analysis(tmp_path, "buggy.py", LOST_BUGGY)

    assert code == EXIT_CONFIRMED
    assert report["analysis_status"] == "executed"
    assert report["selected_scenario"] == "lost-response"
    assert report["scenario_configuration"]["runner"] == "runtime_subprocess"
    assert report["scenario_configuration"]["settings"]["entrypoint"] == "module"
    assert report["verdict"] == "confirmed"
    assert report["execution"]["occurred"] is True
    assert report["execution"]["observed_side_effects"] == 2
    assert len(report["execution"]["attempt_ids"]) == 2
    assert len(report["execution"]["side_effect_ids"]) == 2
    assert report["execution"]["provenance"] == "runtime_observation"
    assert report["verdict_provenance"] == "runtime_verifier"
    assert report["source_file"].endswith("buggy.py")


def test_supported_fixed_lost_response_client_is_not_reproduced(tmp_path) -> None:
    report, code = run_analysis(tmp_path, "fixed.py", LOST_FIXED)

    assert code == EXIT_NOT_REPRODUCED
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    assert report["scenario_configuration"]["settings"][
        "server_supports_idempotency"
    ] is True


def test_non_retrying_client_is_not_falsely_confirmed(tmp_path) -> None:
    report, code = run_analysis(tmp_path, "no_retry.py", NO_RETRY)

    assert code == EXIT_NOT_REPRODUCED
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["occurred"] is True
    assert report["execution"]["observed_side_effects"] == 1
    assert len(report["execution"]["attempt_ids"]) == 1


def test_buggy_retryable_error_client_is_actually_executed(tmp_path) -> None:
    report, code = run_analysis(
        tmp_path,
        "retryable_buggy.py",
        RETRYABLE_BUGGY,
        scenario="retryable-error",
    )

    assert code == EXIT_CONFIRMED
    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    assert any(
        event["kind"] == "retry_scheduled"
        for event in report["execution"]["events"]
    )


def test_fixed_retryable_error_client_is_actually_executed(tmp_path) -> None:
    report, code = run_analysis(
        tmp_path,
        "retryable_fixed.py",
        RETRYABLE_FIXED,
        scenario="retryable-error",
    )

    assert code == EXIT_NOT_REPRODUCED
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    assert any(
        event["kind"] == "idempotent_replay"
        for event in report["execution"]["events"]
    )


def test_nested_client_is_not_executed_or_substituted(tmp_path) -> None:
    report, code = run_analysis(tmp_path, "nested.py", NESTED_CLIENT)

    assert code == EXIT_INCONCLUSIVE
    assert report["analysis_status"] == "not_runnable"
    assert report["verdict"] == "inconclusive"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["events"] == []
    assert report["scenario_configuration"]["executed"] is False
    assert any(
        "module-level" in blocker
        for blocker in report["hypothesis"]["scenario_readiness"]["blockers"]
    )


def test_incomplete_runtime_trace_is_inconclusive(tmp_path) -> None:
    report, code = run_analysis(
        tmp_path, "incomplete.py", INCOMPLETE_TRACE
    )

    assert code == EXIT_INCONCLUSIVE
    assert report["analysis_status"] == "executed"
    assert report["verdict"] == "inconclusive"
    assert report["verdict_provenance"] == "runtime_verifier"
    assert report["execution"]["occurred"] is True
    assert report["execution"]["observed_side_effects"] == 0
    assert all(
        event["kind"] != "request_sent"
        for event in report["execution"]["events"]
    )


def test_runtime_execution_timeout_maps_to_error(monkeypatch, tmp_path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["contractbreaker"], timeout=1)

    monkeypatch.setattr("contractbreaker.runtime.subprocess.run", timeout)

    with pytest.raises(RuntimeExecutionError, match="timed out"):
        run_analyzed_client(
            source=tmp_path / "client.py",
            scenario="lost-response",
            server_supports_idempotency=False,
            execution_timeout_seconds=1.0,
        )

# --- approved-digest boundary ---------------------------------------------


def _capture_worker_command(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        command = list(command)
        captured["command"] = command
        source_path = Path(command[command.index("--source") + 1])
        result_path = Path(command[command.index("--result") + 1])
        captured["source_path"] = source_path
        captured["source_bytes"] = source_path.read_bytes()
        result_path.write_text(
            json.dumps(
                {
                    "events": [],
                    "observed_side_effects": 0,
                    "worker_error": None,
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr("contractbreaker.runtime.subprocess.run", fake_run)
    return captured


def test_expected_digest_mismatch_is_rejected(tmp_path) -> None:
    source = tmp_path / "client.py"
    source.write_text(LOST_BUGGY, encoding="utf-8")

    with pytest.raises(RuntimeExecutionError, match="approved digest"):
        run_analyzed_client(
            source=source,
            scenario="lost-response",
            server_supports_idempotency=False,
            expected_source_sha256="0" * 64,
        )


def test_worker_executes_the_temporary_verified_copy(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "client.py"
    source.write_bytes(LOST_BUGGY.encode("utf-8"))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    captured = _capture_worker_command(monkeypatch)

    run_analyzed_client(
        source=source,
        scenario="lost-response",
        server_supports_idempotency=False,
        expected_source_sha256=digest,
    )

    assert captured["source_path"] != source.resolve()
    assert captured["source_path"].name == "client.py"
    assert captured["source_bytes"] == LOST_BUGGY.encode("utf-8")
    assert str(source.resolve()) not in captured["command"]


def test_worker_receives_the_original_path_without_a_digest(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "client.py"
    source.write_bytes(LOST_BUGGY.encode("utf-8"))
    captured = _capture_worker_command(monkeypatch)

    run_analyzed_client(
        source=source,
        scenario="lost-response",
        server_supports_idempotency=False,
    )

    assert captured["source_path"] == source.resolve()
