"""CLI and JSON report tests for the full ContractBreaker pipeline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from contractbreaker.cli import main
from contractbreaker.report import (
    EXIT_CONFIRMED,
    EXIT_INCONCLUSIVE,
    EXIT_INVALID_ARGS,
    EXIT_NOT_REPRODUCED,
    EXIT_UNSUPPORTED_INPUT,
    SCHEMA_VERSION,
)

BUGGY_SOURCE = """import requests

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

PROTECTED_SOURCE = """import uuid
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

UNKNOWN_LIMIT_SOURCE = """import requests

for attempt in range(max_retries + 1):
    try:
        requests.post(
            "http://service.test/payments",
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

UNSUPPORTED_SOURCE = """import httpx
httpx.post("http://service.test/payments", timeout=1)
"""

AMBIGUOUS_KEY_SOURCE = """import requests

def send(use_key, key):
    headers = {}
    if use_key:
        headers["Idempotency-Key"] = key
    for attempt in range(2):
        try:
            requests.post(
                "http://service.test/payments",
                headers=headers,
                timeout=5,
            )
        except requests.exceptions.Timeout:
            continue
"""


def write_source(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def run_cli(args: list[str], capsys) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def parse_json_output(output: str) -> dict:
    return json.loads(output)


def test_buggy_lost_response_cli_is_confirmed(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code, out, err = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "lost-response",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == EXIT_CONFIRMED
    assert err == ""
    report = parse_json_output(out)
    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    assert report["execution"]["occurred"] is True
    assert report["verdict_provenance"] == "runtime_verifier"
    assert report["scenario_configuration"]["settings"]["max_retries"] == 1


def test_protected_lost_response_cli_is_not_reproduced(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "protected.py", PROTECTED_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )

    assert code == EXIT_NOT_REPRODUCED
    report = parse_json_output(out)
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    settings = report["scenario_configuration"]["settings"]
    assert settings["client_sends_idempotency_key"] is True
    assert settings["server_supports_idempotency"] is True


def test_buggy_retryable_error_cli_is_confirmed(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code, out, _ = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "retryable-error",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == EXIT_CONFIRMED
    report = parse_json_output(out)
    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    settings = report["scenario_configuration"]["settings"]
    assert settings["retry_statuses"] == [503]
    assert settings["server_error_status"] == 503
    provenance = report["scenario_configuration"]["provenance"]
    assert provenance["retry_statuses"] == "cli_default"
    assert provenance["server_error_status"] == "cli_default"


def test_protected_retryable_error_cli_is_not_reproduced(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "protected.py", PROTECTED_SOURCE)

    code, out, _ = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "retryable-error",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == EXIT_NOT_REPRODUCED
    report = parse_json_output(out)
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    assert (
        report["scenario_configuration"]["settings"][
            "server_supports_idempotency"
        ]
        is True
    )


def test_auto_selects_runnable_lost_response(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "auto", "--format", "json"],
        capsys,
    )

    assert code == EXIT_CONFIRMED
    report = parse_json_output(out)
    assert report["scenario"] == "auto"
    assert report["selected_scenario"] == "lost-response"
    assert report["verdict"] == "confirmed"


def test_unknown_retry_limit_is_inconclusive_and_not_zero(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "unknown.py", UNKNOWN_LIMIT_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )

    assert code == EXIT_INCONCLUSIVE
    assert code != EXIT_NOT_REPRODUCED
    report = parse_json_output(out)
    assert report["analysis_status"] == "not_runnable"
    assert report["verdict"] == "inconclusive"
    assert report["verdict_provenance"] == "not_executed"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["events"] == []
    assert any(
        "retry count" in blocker
        for blocker in report["hypothesis"]["scenario_readiness"]["blockers"]
    )


def test_unknown_timeout_is_inconclusive_and_not_zero(tmp_path, capsys) -> None:
    source = write_source(
        tmp_path,
        "unknown_timeout.py",
        """import requests

configured_timeout = 5
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            timeout=configured_timeout,
        )
    except requests.exceptions.Timeout:
        continue
""",
    )

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )

    assert code == EXIT_INCONCLUSIVE
    report = parse_json_output(out)
    assert report["verdict"] == "inconclusive"
    assert report["execution"]["occurred"] is False
    assert report["hypothesis"]["timeout"]["status"] == "unknown"


def test_text_output_distinguishes_not_runnable(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "unknown.py", UNKNOWN_LIMIT_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response"],
        capsys,
    )

    assert code == EXIT_INCONCLUSIVE
    assert "Verdict: INCONCLUSIVE" in out
    assert "potential conflict not evaluated" in out.lower()
    assert "none; no runtime scenario was executed" in out


def test_unsupported_source_is_structured_and_never_confirmed(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "unsupported.py", UNSUPPORTED_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )

    assert code == EXIT_UNSUPPORTED_INPUT
    report = parse_json_output(out)
    assert report["analysis_status"] == "unsupported_input"
    assert report["verdict"] == "inconclusive"
    assert report["verdict_provenance"] == "not_executed"
    assert report["execution"]["occurred"] is False
    assert report["extraction"]["status"] == "unsupported"
    assert any(
        warning["code"] == "unsupported.http_client"
        for warning in report["extraction"]["warnings"]
    )


def test_ambiguous_idempotency_key_is_not_treated_as_safe(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "ambiguous.py", AMBIGUOUS_KEY_SOURCE)

    code, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )

    assert code == EXIT_INCONCLUSIVE
    report = parse_json_output(out)
    assert report["verdict"] == "inconclusive"
    assert report["verdict_provenance"] == "not_executed"
    assert (
        report["hypothesis"]["idempotency_key_present"]["status"] == "ambiguous"
    )
    assert report["hypothesis"]["scenario_readiness"]["runnable"] is False
    assert report["execution"]["occurred"] is False


def test_invalid_scenario_exits_three(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    with pytest.raises(SystemExit) as error:
        main(["analyze", str(source), "--scenario", "not-a-scenario"])

    assert error.value.code == EXIT_INVALID_ARGS


def test_invalid_cli_arguments_exit_three(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    with pytest.raises(SystemExit) as missing_source:
        main(["analyze"])
    assert missing_source.value.code == EXIT_INVALID_ARGS

    with pytest.raises(SystemExit) as bad_status:
        main(
            [
                "analyze",
                str(source),
                "--scenario",
                "retryable-error",
                "--retry-status",
                "200",
            ]
        )
    assert bad_status.value.code == EXIT_INVALID_ARGS

    with pytest.raises(SystemExit) as wrong_scenario:
        main(
            [
                "analyze",
                str(source),
                "--scenario",
                "lost-response",
                "--retry-status",
                "503",
            ]
        )
    assert wrong_scenario.value.code == EXIT_INVALID_ARGS


def test_output_file_is_json_even_with_text_stdout(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)
    output = tmp_path / "report.json"

    code, out, _ = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "lost-response",
            "--output",
            str(output),
        ],
        capsys,
    )

    assert code == EXIT_CONFIRMED
    assert "Verdict: CONFIRMED" in out
    report = parse_json_output(output.read_text(encoding="utf-8"))
    assert report["verdict"] == "confirmed"
    assert report["schema_version"] == SCHEMA_VERSION


def test_report_schema_is_stable_and_json_native(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    _, out, _ = run_cli(
        ["analyze", str(source), "--scenario", "lost-response", "--format", "json"],
        capsys,
    )
    report = parse_json_output(out)

    assert set(report) == {
        "schema_version",
        "tool_version",
        "analysis_status",
        "scenario",
        "selected_scenario",
        "source_file",
        "extraction",
        "hypothesis",
        "scenario_configuration",
        "execution",
        "verdict",
        "verdict_reason",
        "verdict_provenance",
        "limitations",
    }
    assert report["schema_version"] == SCHEMA_VERSION
    assert isinstance(report["tool_version"], str)
    assert set(report["execution"]) == {
        "occurred",
        "provenance",
        "runner",
        "events",
        "attempt_ids",
        "operation_ids",
        "side_effect_ids",
        "observed_side_effects",
    }
    assert report["execution"]["provenance"] == "runtime_observation"
    assert report["extraction"]["provenance"] == "static_extraction"
    assert report["hypothesis"]["provenance"] == "derived_from_extraction"
    assert report["execution"]["observed_side_effects"] == 2
    assert len(report["execution"]["attempt_ids"]) == 2
    assert len(report["execution"]["side_effect_ids"]) == 2

    # The report can round-trip without leaking custom objects.
    json.loads(json.dumps(report, sort_keys=True))


def test_retry_policy_can_be_explicitly_configured(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code, out, _ = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "retryable-error",
            "--retry-status",
            "500",
            "--retry-status",
            "503",
            "--server-error-status",
            "503",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == EXIT_CONFIRMED
    report = parse_json_output(out)
    settings = report["scenario_configuration"]["settings"]
    assert settings["retry_statuses"] == [500, 503]
    provenance = report["scenario_configuration"]["provenance"]
    assert provenance["retry_statuses"] == "cli"
    assert provenance["server_error_status"] == "cli"


def test_retry_policy_not_covering_server_error_is_not_confirmed(
    tmp_path, capsys
) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code, out, _ = run_cli(
        [
            "analyze",
            str(source),
            "--scenario",
            "retryable-error",
            "--retry-status",
            "500",
            "--server-error-status",
            "503",
            "--format",
            "json",
        ],
        capsys,
    )

    assert code == EXIT_NOT_REPRODUCED
    report = parse_json_output(out)
    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    assert any(
        "no-retry configuration" in limitation
        for limitation in report["limitations"]
    )


def test_missing_source_file_exits_four(tmp_path) -> None:
    code = main(["analyze", str(tmp_path / "missing.py")])
    assert code == EXIT_UNSUPPORTED_INPUT


def test_output_write_failure_exits_four(tmp_path, capsys) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    code = main(
        [
            "analyze",
            str(source),
            "--scenario",
            "lost-response",
            "--output",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_UNSUPPORTED_INPUT
    assert "could not write report" in captured.err


def test_module_entrypoint_works_outside_repo(tmp_path) -> None:
    source = write_source(tmp_path, "buggy.py", BUGGY_SOURCE)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "contractbreaker",
            "analyze",
            str(source),
            "--scenario",
            "lost-response",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == EXIT_CONFIRMED, result.stderr
    report = json.loads(result.stdout)
    assert report["verdict"] == "confirmed"