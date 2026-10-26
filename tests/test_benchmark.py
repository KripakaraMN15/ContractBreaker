"""Tests for benchmark metrics, dataset loading, and CLI behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contractbreaker.benchmark import (
    EXIT_BENCHMARK_ERROR,
    EXIT_BENCHMARK_MISMATCH,
    EXIT_BENCHMARK_OK,
    BenchmarkDatasetError,
    benchmark_exit_code,
    compute_metrics,
    load_benchmark_dataset,
    run_benchmark,
)
from contractbreaker.cli import main

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

UNSUPPORTED_SOURCE = """import httpx
httpx.post("http://service.test/payments", timeout=1)
"""


def fact(status: str, value=None) -> dict:
    return {"status": status, "value": value}


def supported_buggy_expected() -> dict:
    return {
        "extraction_status": "supported",
        "facts": {
            "method": fact("known", "POST"),
            "endpoint": fact("known", "http://service.test/payments"),
            "timeout": fact("known", 5),
            "retry_count": fact("known", 1),
            "retry_triggers": fact(
                "known", ["requests.exceptions.Timeout"]
            ),
            "idempotency_key_present": fact("absent"),
            "idempotency_key_reused": fact("absent"),
        },
        "warning_codes": [],
        "runnable": True,
        "execution_expected": True,
        "verdict": "confirmed",
        "side_effects": 2,
    }


def unsupported_expected() -> dict:
    return {
        "extraction_status": "unsupported",
        "facts": {},
        "warning_codes": [
            "unsupported.http_client",
            "unsupported.http_call",
        ],
        "runnable": False,
        "execution_expected": False,
        "verdict": "inconclusive",
        "side_effects": None,
    }


def write_case(
    root: Path,
    split: str,
    case_id: str,
    source_name: str,
    source: str,
    scenario: str,
    expected: dict,
) -> dict:
    split_dir = root / split
    split_dir.mkdir(parents=True, exist_ok=True)
    (split_dir / source_name).write_text(source, encoding="utf-8")
    return {
        "id": case_id,
        "source": source_name,
        "scenario": scenario,
        "inputs": {"server_idempotency": "auto", "execution_timeout": 5.0},
        "expected": expected,
        "notes": "test case",
    }


def write_dataset(
    root: Path,
    development_cases: list[dict],
    held_out_cases: list[dict],
    *,
    dataset_version: str = "1.0",
) -> None:
    for split, cases in (
        ("development", development_cases),
        ("held_out", held_out_cases),
    ):
        split_dir = root / split
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "dataset": "test-dataset",
                    "dataset_version": dataset_version,
                    "split": split,
                    "cases": cases,
                }
            ),
            encoding="utf-8",
        )


def metric_case(
    *,
    expected: dict,
    actual: dict,
    checks: dict,
    error: str | None = None,
) -> dict:
    return {
        "expected": expected,
        "actual": actual,
        "checks": checks,
        "error": error,
    }


def checks_for(
    *,
    facts: dict[str, bool] | None = None,
    extraction_status: bool = True,
    warning_codes: bool = True,
    runnable: bool = True,
    execution: bool = True,
    verdict: bool = True,
    side_effects: bool = True,
) -> dict:
    return {
        "extraction_status": extraction_status,
        "warning_codes": warning_codes,
        "facts": facts or {},
        "runnable": runnable,
        "execution": execution,
        "verdict": verdict,
        "side_effects": side_effects,
    }


def test_known_metric_calculations() -> None:
    timeout_fact = fact("known", 5)
    common_expected = {
        "extraction_status": "supported",
        "facts": {"timeout": timeout_fact},
        "warning_codes": [],
        "runnable": True,
        "execution_expected": True,
        "verdict": "confirmed",
        "side_effects": 2,
    }
    fixed_expected = {
        **common_expected,
        "verdict": "not_reproduced",
        "side_effects": 1,
    }
    blocked_expected = {
        "extraction_status": "unsupported",
        "facts": {},
        "warning_codes": ["unsupported.http_client"],
        "runnable": False,
        "execution_expected": False,
        "verdict": "inconclusive",
        "side_effects": None,
    }
    blocked_partial_expected = {
        **blocked_expected,
        "extraction_status": "partial",
        "warning_codes": [],
    }
    cases = [
        metric_case(
            expected=common_expected,
            actual={
                "runnable": True,
                "executed": True,
                "verdict": "confirmed",
                "side_effects": 2,
            },
            checks=checks_for(facts={"timeout": True}),
        ),
        metric_case(
            expected=fixed_expected,
            actual={
                "runnable": True,
                "executed": True,
                "verdict": "not_reproduced",
                "side_effects": 1,
            },
            checks=checks_for(facts={"timeout": True}),
        ),
        metric_case(
            expected=fixed_expected,
            actual={
                "runnable": True,
                "executed": True,
                "verdict": "confirmed",
                "side_effects": 2,
            },
            checks=checks_for(
                facts={"timeout": True},
                verdict=False,
                side_effects=False,
            ),
        ),
        metric_case(
            expected=blocked_expected,
            actual={
                "runnable": False,
                "executed": False,
                "verdict": "inconclusive",
                "side_effects": None,
            },
            checks=checks_for(
                warning_codes=True,
                runnable=True,
                execution=True,
                verdict=True,
                side_effects=True,
            ),
        ),
        metric_case(
            expected=blocked_partial_expected,
            actual={
                "runnable": True,
                "executed": True,
                "verdict": "confirmed",
                "side_effects": 2,
            },
            checks=checks_for(
                warning_codes=True,
                runnable=False,
                execution=False,
                verdict=False,
                side_effects=False,
            ),
        ),
    ]

    metrics = compute_metrics(cases)

    assert metrics["case_count"] == 5
    assert metrics["extraction"]["status_accuracy"]["correct"] == 5
    assert metrics["extraction"]["fact_accuracy"]["timeout"] == {
        "correct": 3,
        "total": 3,
        "rate": 1.0,
    }
    assert metrics["extraction"]["uncertainty_accuracy"] == {
        "correct": 1,
        "total": 1,
        "rate": 1.0,
    }
    assert metrics["runnability"] == {
        "correctly_runnable": 3,
        "correctly_blocked": 1,
        "incorrectly_allowed": 1,
        "incorrectly_blocked": 0,
        "accuracy": {"correct": 4, "total": 5, "rate": 0.8},
    }
    assert metrics["runtime"]["detection_rate"] == {
        "correct": 1,
        "total": 1,
        "rate": 1.0,
    }
    assert metrics["runtime"]["false_positive_rate"] == {
        "correct": 1,
        "total": 2,
        "rate": 0.5,
    }
    assert metrics["runtime"]["side_effect_accuracy"] == {
        "correct": 2,
        "total": 3,
        "rate": 0.6667,
    }
    assert metrics["runtime"]["inconclusive_cases"] == 1
    assert metrics["runtime"]["executed_verdict_accuracy"] == {
        "correct": 2,
        "total": 4,
        "rate": 0.5,
    }
    assert metrics["runtime"]["unexpected_executions"] == 1


def test_labels_do_not_change_runtime_verdict(tmp_path) -> None:
    source = write_case(
        tmp_path,
        "held_out",
        "held-wrong-label",
        "buggy.py",
        BUGGY_SOURCE,
        "lost-response",
        {**supported_buggy_expected(), "verdict": "not_reproduced", "side_effects": 1},
    )
    development = write_case(
        tmp_path,
        "development",
        "dev-unsupported",
        "unsupported.py",
        UNSUPPORTED_SOURCE,
        "lost-response",
        unsupported_expected(),
    )
    write_dataset(tmp_path, [development], [source])

    report = run_benchmark(tmp_path)
    case = report["splits"]["held_out"]["cases"][0]

    assert report["all_matched"] is False
    assert case["actual"]["verdict"] == "confirmed"
    assert case["actual"]["side_effects"] == 2
    assert case["matched"] is False
    assert "verdict" in case["mismatches"]
    assert "side_effects" in case["mismatches"]
    assert benchmark_exit_code(report) == EXIT_BENCHMARK_MISMATCH


def test_benchmark_cli_json_and_unsupported_case_not_executed(
    tmp_path, capsys
) -> None:
    development = write_case(
        tmp_path,
        "development",
        "dev-buggy",
        "buggy.py",
        BUGGY_SOURCE,
        "lost-response",
        supported_buggy_expected(),
    )
    held = write_case(
        tmp_path,
        "held_out",
        "held-unsupported",
        "unsupported.py",
        UNSUPPORTED_SOURCE,
        "lost-response",
        unsupported_expected(),
    )
    write_dataset(tmp_path, [development], [held])
    output = tmp_path / "benchmark.json"

    code = main(
        [
            "benchmark",
            "--dataset",
            str(tmp_path),
            "--format",
            "json",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_BENCHMARK_OK
    report = json.loads(captured.out)
    assert set(report["splits"]) == {"development", "held_out"}
    assert report["splits"]["development"]["cases"][0]["actual"]["executed"] is True
    assert report["splits"]["held_out"]["cases"][0]["actual"]["executed"] is False
    assert report["splits"]["held_out"]["cases"][0]["actual"]["verdict"] == "inconclusive"
    assert json.loads(output.read_text(encoding="utf-8"))["all_matched"] is True


def test_missing_dataset_exits_error(tmp_path, capsys) -> None:
    code = main(
        [
            "benchmark",
            "--dataset",
            str(tmp_path / "missing"),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert code == EXIT_BENCHMARK_ERROR
    assert "dataset directory not found" in captured.err


def test_invalid_manifest_exits_error(tmp_path, capsys) -> None:
    split_dir = tmp_path / "development"
    split_dir.mkdir()
    (split_dir / "manifest.json").write_text("{broken", encoding="utf-8")
    held_dir = tmp_path / "held_out"
    held_dir.mkdir()
    (held_dir / "manifest.json").write_text('{"cases": []}', encoding="utf-8")

    code = main(
        ["benchmark", "--dataset", str(tmp_path), "--format", "json"]
    )

    captured = capsys.readouterr()
    assert code == EXIT_BENCHMARK_ERROR
    assert "could not read development manifest" in captured.err


def test_benchmark_requires_dataset_argument() -> None:
    with pytest.raises(SystemExit) as error:
        main(["benchmark"])

    assert error.value.code == 3


def test_benchmark_records_case_errors(tmp_path) -> None:
    bad_case = write_case(
        tmp_path,
        "development",
        "dev-bad-inputs",
        "buggy.py",
        BUGGY_SOURCE,
        "lost-response",
        supported_buggy_expected(),
    )
    bad_case["inputs"] = {"retry_statuses": [503]}
    held = write_case(
        tmp_path,
        "held_out",
        "held-unsupported",
        "unsupported.py",
        UNSUPPORTED_SOURCE,
        "lost-response",
        unsupported_expected(),
    )
    write_dataset(tmp_path, [bad_case], [held])

    report = run_benchmark(tmp_path)
    case = report["splits"]["development"]["cases"][0]

    assert report["all_matched"] is False
    assert case["actual"] is None
    assert "AnalysisUsageError" in (case["error"] or "")
    assert case["matched"] is False
    assert report["splits"]["development"]["metrics"]["runtime"]["case_errors"] == 1
    assert benchmark_exit_code(report) == EXIT_BENCHMARK_MISMATCH


def test_dataset_loader_rejects_source_outside_split(tmp_path) -> None:
    external = tmp_path / "outside.py"
    external.write_text(BUGGY_SOURCE, encoding="utf-8")
    development = {
        "id": "dev-escape",
        "source": "../outside.py",
        "scenario": "lost-response",
        "inputs": {},
        "expected": supported_buggy_expected(),
        "notes": "bad",
    }
    held = write_case(
        tmp_path,
        "held_out",
        "held-unsupported",
        "unsupported.py",
        UNSUPPORTED_SOURCE,
        "lost-response",
        unsupported_expected(),
    )
    write_dataset(tmp_path, [development], [held])

    with pytest.raises(BenchmarkDatasetError, match="escapes"):
        load_benchmark_dataset(tmp_path)