"""Reproducible synthetic benchmark for ContractBreaker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .pipeline import AnalysisOptions, analyze
from .version import __version__

BENCHMARK_SCHEMA_VERSION = "1.0"
EXIT_BENCHMARK_OK = 0
EXIT_BENCHMARK_MISMATCH = 1
EXIT_BENCHMARK_ERROR = 2

SPLITS = ("development", "held_out")
_FACT_NAMES = (
    "method",
    "endpoint",
    "timeout",
    "retry_count",
    "retry_triggers",
    "idempotency_key_present",
    "idempotency_key_reused",
)
_EXTRACTION_STATUSES = {
    "supported",
    "partial",
    "unsupported",
    "no_http_calls",
    "parse_error",
}
_FACT_STATUSES = {"known", "absent", "unknown", "ambiguous", "unsupported"}
_VERDICTS = {"confirmed", "not_reproduced", "inconclusive"}
_INPUT_KEYS = {
    "server_idempotency",
    "retry_statuses",
    "server_error_status",
    "execution_timeout",
}


class BenchmarkDatasetError(ValueError):
    """Raised when a benchmark dataset can not be loaded safely."""


@dataclass(frozen=True)
class BenchmarkCase:
    """One labeled benchmark case."""

    case_id: str
    split: str
    source: str
    source_path: Path
    scenario: str
    inputs: dict[str, Any]
    expected: dict[str, Any]
    notes: str = ""


@dataclass(frozen=True)
class BenchmarkDataset:
    """A development and held-out dataset pair."""

    name: str
    version: str
    splits: dict[str, tuple[BenchmarkCase, ...]]


def load_benchmark_dataset(root: str | Path) -> BenchmarkDataset:
    """Load and validate ``development`` and ``held_out`` manifests."""

    dataset_root = Path(root).resolve()
    if not dataset_root.is_dir():
        raise BenchmarkDatasetError(f"dataset directory not found: {dataset_root}")

    cases_by_split: dict[str, tuple[BenchmarkCase, ...]] = {}
    dataset_name: str | None = None
    dataset_version: str | None = None
    seen_ids: set[str] = set()

    for split in SPLITS:
        split_dir = dataset_root / split
        manifest_path = split_dir / "manifest.json"
        if not manifest_path.is_file():
            raise BenchmarkDatasetError(f"missing {split} manifest: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkDatasetError(
                f"could not read {split} manifest: {exc}"
            ) from exc

        name = _require_text(manifest.get("dataset"), "dataset")
        version = _require_text(manifest.get("dataset_version"), "dataset_version")
        if dataset_name is None:
            dataset_name = name
            dataset_version = version
        elif (name, version) != (dataset_name, dataset_version):
            raise BenchmarkDatasetError(
                "development and held_out manifests have different dataset/version"
            )
        if manifest.get("split") != split:
            raise BenchmarkDatasetError(
                f"{split} manifest has split={manifest.get('split')!r}"
            )
        cases = manifest.get("cases")
        if not isinstance(cases, list):
            raise BenchmarkDatasetError(f"{split} manifest cases must be a list")

        loaded: list[BenchmarkCase] = []
        for raw in cases:
            case = _load_case(raw, split, split_dir)
            if case.case_id in seen_ids:
                raise BenchmarkDatasetError(
                    f"duplicate benchmark case id: {case.case_id}"
                )
            seen_ids.add(case.case_id)
            loaded.append(case)
        cases_by_split[split] = tuple(loaded)

    if dataset_name is None or dataset_version is None:
        raise BenchmarkDatasetError("dataset metadata is missing")
    return BenchmarkDataset(
        name=dataset_name,
        version=dataset_version,
        splits=cases_by_split,
    )


def run_benchmark_dataset(dataset: BenchmarkDataset) -> dict[str, Any]:
    """Run every case and return a development/held-out benchmark report."""

    split_reports: dict[str, Any] = {}
    all_matched = True
    for split in SPLITS:
        cases = dataset.splits[split]
        results = [_run_case(case) for case in cases]
        metrics = compute_metrics(results)
        if not all(result["matched"] for result in results):
            all_matched = False
        split_reports[split] = {
            "split": split,
            "case_count": len(results),
            "metrics": metrics,
            "cases": results,
        }

    return {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "tool_version": __version__,
        "dataset": dataset.name,
        "dataset_version": dataset.version,
        "all_matched": all_matched,
        "splits": split_reports,
    }


def run_benchmark(root: str | Path) -> dict[str, Any]:
    """Load a dataset root and run it."""

    return run_benchmark_dataset(load_benchmark_dataset(root))


def compute_metrics(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute extraction, runnability, and runtime metrics."""

    total = len(case_results)
    extraction_status = _metric(
        sum(1 for case in case_results if case["checks"]["extraction_status"]),
        total,
    )
    warning_codes = _metric(
        sum(1 for case in case_results if case["checks"]["warning_codes"]),
        total,
    )

    fact_metrics: dict[str, Any] = {}
    for fact in _FACT_NAMES:
        labelled = [
            case for case in case_results if fact in case["expected"]["facts"]
        ]
        fact_metrics[fact] = _metric(
            sum(1 for case in labelled if case["checks"]["facts"].get(fact)),
            len(labelled),
        )

    uncertain_correct = 0
    uncertain_total = 0
    unsupported_input_correct = 0
    unsupported_input_total = 0
    for case in case_results:
        expected = case["expected"]
        if expected["extraction_status"] in {
            "unsupported",
            "no_http_calls",
            "parse_error",
        }:
            unsupported_input_total += 1
            if case["checks"]["extraction_status"]:
                unsupported_input_correct += 1
        if expected["extraction_status"] == "unsupported":
            uncertain_total += 1
            if case["checks"]["extraction_status"]:
                uncertain_correct += 1
        for fact, fact_label in expected["facts"].items():
            if fact_label["status"] in {"unknown", "ambiguous", "unsupported"}:
                uncertain_total += 1
                if case["checks"]["facts"].get(fact):
                    uncertain_correct += 1

    correctly_runnable = 0
    correctly_blocked = 0
    incorrectly_allowed = 0
    incorrectly_blocked = 0
    for case in case_results:
        expected_runnable = bool(case["expected"]["runnable"])
        actual_runnable = bool((case.get("actual") or {}).get("runnable"))
        if expected_runnable and actual_runnable:
            correctly_runnable += 1
        elif not expected_runnable and not actual_runnable:
            correctly_blocked += 1
        elif not expected_runnable and actual_runnable:
            incorrectly_allowed += 1
        else:
            incorrectly_blocked += 1

    buggy_cases = [
        case
        for case in case_results
        if case["expected"]["runnable"]
        and case["expected"]["verdict"] == "confirmed"
    ]
    fixed_cases = [
        case
        for case in case_results
        if case["expected"]["runnable"]
        and case["expected"]["verdict"] == "not_reproduced"
    ]
    detections = sum(
        1
        for case in buggy_cases
        if (case.get("actual") or {}).get("verdict") == "confirmed"
    )
    false_positives = sum(
        1
        for case in fixed_cases
        if (case.get("actual") or {}).get("verdict") == "confirmed"
    )
    side_effect_cases = [
        case
        for case in case_results
        if case["expected"]["side_effects"] is not None
    ]
    side_effects_correct = sum(
        1
        for case in side_effect_cases
        if (case.get("actual") or {}).get("side_effects")
        == case["expected"]["side_effects"]
    )
    executed_verdict_cases = [
        case
        for case in case_results
        if (case.get("actual") or {}).get("executed")
    ]
    verdict_correct = sum(
        1
        for case in case_results
        if (case.get("actual") or {}).get("verdict")
        == case["expected"]["verdict"]
    )
    executed_verdict_correct = sum(
        1
        for case in executed_verdict_cases
        if case["actual"]["verdict"] == case["expected"]["verdict"]
    )
    inconclusive = sum(
        1
        for case in case_results
        if (case.get("actual") or {}).get("verdict") == "inconclusive"
    )
    execution_failures = sum(
        1
        for case in case_results
        if case["expected"]["execution_expected"]
        and not (case.get("actual") or {}).get("executed", False)
    )
    unexpected_executions = sum(
        1
        for case in case_results
        if not case["expected"]["execution_expected"]
        and (case.get("actual") or {}).get("executed", False)
    )
    case_errors = sum(1 for case in case_results if case.get("error"))

    return {
        "case_count": total,
        "extraction": {
            "status_accuracy": extraction_status,
            "warning_code_accuracy": warning_codes,
            "fact_accuracy": fact_metrics,
            "uncertainty_accuracy": _metric(
                uncertain_correct, uncertain_total
            ),
            "unsupported_input_accuracy": _metric(
                unsupported_input_correct, unsupported_input_total
            ),
        },
        "runnability": {
            "correctly_runnable": correctly_runnable,
            "correctly_blocked": correctly_blocked,
            "incorrectly_allowed": incorrectly_allowed,
            "incorrectly_blocked": incorrectly_blocked,
            "accuracy": _metric(
                correctly_runnable + correctly_blocked, total
            ),
        },
        "runtime": {
            "supported_buggy_cases": len(buggy_cases),
            "detected_buggy_cases": detections,
            "detection_rate": _metric(detections, len(buggy_cases)),
            "supported_fixed_cases": len(fixed_cases),
            "false_positive_cases": false_positives,
            "false_positive_rate": _metric(false_positives, len(fixed_cases)),
            "side_effect_checks": len(side_effect_cases),
            "correct_side_effect_counts": side_effects_correct,
            "side_effect_accuracy": _metric(
                side_effects_correct, len(side_effect_cases)
            ),
            "inconclusive_cases": inconclusive,
            "verdict_accuracy": _metric(verdict_correct, total),
            "executed_case_count": len(executed_verdict_cases),
            "executed_verdict_accuracy": _metric(
                executed_verdict_correct, len(executed_verdict_cases)
            ),
            "execution_failures": execution_failures,
            "unexpected_executions": unexpected_executions,
            "case_errors": case_errors,
        },
    }


def benchmark_exit_code(report: dict[str, Any]) -> int:
    """Return 0 only when every case matched its labeled expectations."""

    return EXIT_BENCHMARK_OK if report.get("all_matched") else EXIT_BENCHMARK_MISMATCH


def benchmark_report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def write_benchmark_report(report: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(benchmark_report_to_json(report), encoding="utf-8")


def render_benchmark_text(report: dict[str, Any]) -> str:
    """Render a concise split-by-split benchmark summary."""

    lines = [
        "ContractBreaker benchmark",
        f"Dataset: {report['dataset']} v{report['dataset_version']}",
        f"All labeled cases matched: {'yes' if report['all_matched'] else 'no'}",
    ]
    for split in SPLITS:
        split_report = report["splits"][split]
        metrics = split_report["metrics"]
        extraction = metrics["extraction"]
        runnability = metrics["runnability"]
        runtime = metrics["runtime"]
        lines.append("")
        lines.append(f"{split} ({split_report['case_count']} cases)")
        lines.append(
            f"  Extraction status: {_rate(extraction['status_accuracy'])}; "
            f"warnings: {_rate(extraction['warning_code_accuracy'])}; "
            f"uncertainty: {_rate(extraction['uncertainty_accuracy'])}; "
            f"unsupported inputs: "
            f"{_rate(extraction['unsupported_input_accuracy'])}"
        )
        fact_parts = [
            f"{name}={_rate(metric)}"
            for name, metric in extraction["fact_accuracy"].items()
            if metric["total"]
        ]
        lines.append("  Facts: " + (", ".join(fact_parts) or "none labeled"))
        lines.append(
            "  Runnability: "
            f"runnable={runnability['correctly_runnable']}, "
            f"blocked={runnability['correctly_blocked']}, "
            f"incorrectly_allowed={runnability['incorrectly_allowed']}, "
            f"incorrectly_blocked={runnability['incorrectly_blocked']}, "
            f"accuracy={_rate(runnability['accuracy'])}"
        )
        lines.append(
            "  Runtime: "
            f"detection={_rate(runtime['detection_rate'])}, "
            f"false_positives={_rate(runtime['false_positive_rate'])}, "
            f"side_effects={_rate(runtime['side_effect_accuracy'])}, "
            f"verdict_accuracy={_rate(runtime['verdict_accuracy'])}, "
            f"executed_verdict_accuracy="
            f"{_rate(runtime['executed_verdict_accuracy'])}, "
            f"inconclusive={runtime['inconclusive_cases']}, "
            f"execution_failures={runtime['execution_failures']}, "
            f"case_errors={runtime['case_errors']}"
        )
        inconclusive_ids = [
            case["id"]
            for case in split_report["cases"]
            if (case.get("actual") or {}).get("verdict") == "inconclusive"
        ]
        if inconclusive_ids:
            lines.append("  Inconclusive cases: " + ", ".join(inconclusive_ids))
        unsupported_ids = [
            case["id"]
            for case in split_report["cases"]
            if case["expected"]["extraction_status"]
            in {"unsupported", "no_http_calls", "parse_error"}
        ]
        if unsupported_ids:
            lines.append(
                "  Unsupported/invalid cases: " + ", ".join(unsupported_ids)
            )
        mismatches = [
            case for case in split_report["cases"] if not case["matched"]
        ]
        if mismatches:
            lines.append("  Mismatches:")
            for case in mismatches:
                lines.append(
                    f"    {case['id']}: {', '.join(case['mismatches'])}"
                )
        else:
            lines.append("  Mismatches: none")
    return "\n".join(lines)


def _load_case(
    raw: object, split: str, split_dir: Path
) -> BenchmarkCase:
    if not isinstance(raw, dict):
        raise BenchmarkDatasetError(f"{split} case must be an object")
    case_id = _require_text(raw.get("id"), "case id")
    source = _require_text(raw.get("source"), f"{case_id} source")
    scenario = raw.get("scenario")
    if scenario not in {"lost-response", "retryable-error"}:
        raise BenchmarkDatasetError(f"{case_id} has invalid scenario {scenario!r}")
    source_path = (split_dir / source).resolve()
    if not source_path.is_relative_to(split_dir.resolve()):
        raise BenchmarkDatasetError(f"{case_id} source escapes its split directory")
    if not source_path.is_file():
        raise BenchmarkDatasetError(f"{case_id} source file not found: {source_path}")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict) or set(inputs) - _INPUT_KEYS:
        raise BenchmarkDatasetError(f"{case_id} has invalid inputs")
    expected = _validate_expected(raw.get("expected"), case_id)
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise BenchmarkDatasetError(f"{case_id} notes must be a string")
    return BenchmarkCase(
        case_id=case_id,
        split=split,
        source=source,
        source_path=source_path,
        scenario=scenario,
        inputs=inputs,
        expected=expected,
        notes=notes,
    )


def _validate_expected(raw: object, case_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BenchmarkDatasetError(f"{case_id} has no expected block")
    required = {
        "extraction_status",
        "facts",
        "warning_codes",
        "runnable",
        "execution_expected",
        "verdict",
        "side_effects",
    }
    if set(raw) != required:
        raise BenchmarkDatasetError(
            f"{case_id} expected fields must be {sorted(required)}"
        )
    if raw["extraction_status"] not in _EXTRACTION_STATUSES:
        raise BenchmarkDatasetError(f"{case_id} has invalid extraction status")
    if not isinstance(raw["facts"], dict):
        raise BenchmarkDatasetError(f"{case_id} facts must be an object")
    for fact, label in raw["facts"].items():
        if fact not in _FACT_NAMES:
            raise BenchmarkDatasetError(f"{case_id} has unknown fact {fact!r}")
        if not isinstance(label, dict) or label.get("status") not in _FACT_STATUSES:
            raise BenchmarkDatasetError(f"{case_id} fact {fact} has invalid status")
    if not isinstance(raw["warning_codes"], list) or any(
        not isinstance(code, str) for code in raw["warning_codes"]
    ):
        raise BenchmarkDatasetError(f"{case_id} warning_codes must be strings")
    if not isinstance(raw["runnable"], bool):
        raise BenchmarkDatasetError(f"{case_id} runnable must be a boolean")
    if not isinstance(raw["execution_expected"], bool):
        raise BenchmarkDatasetError(f"{case_id} execution_expected must be a boolean")
    if raw["verdict"] not in _VERDICTS:
        raise BenchmarkDatasetError(f"{case_id} has invalid verdict")
    side_effects = raw["side_effects"]
    if side_effects is not None and (
        not isinstance(side_effects, int)
        or isinstance(side_effects, bool)
        or side_effects < 0
    ):
        raise BenchmarkDatasetError(f"{case_id} has invalid side_effects")
    return raw


def _run_case(case: BenchmarkCase) -> dict[str, Any]:
    actual: dict[str, Any] | None = None
    error: str | None = None
    try:
        report = analyze(
            AnalysisOptions(
                source=case.source_path,
                scenario=case.scenario,
                server_idempotency=case.inputs.get("server_idempotency", "auto"),
                retry_statuses=(
                    tuple(case.inputs["retry_statuses"])
                    if case.inputs.get("retry_statuses") is not None
                    else None
                ),
                server_error_status=case.inputs.get("server_error_status"),
                execution_timeout=case.inputs.get("execution_timeout"),
            )
        )
        actual = _actual_from_report(report)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if actual is None:
        checks = {
            "extraction_status": False,
            "warning_codes": False,
            "facts": {
                fact: False for fact in case.expected["facts"]
            },
            "runnable": False,
            "execution": False,
            "verdict": False,
            "side_effects": False,
        }
        mismatches = ["analysis error"]
        matched = False
    else:
        checks, mismatches = _compare_case(case.expected, actual)
        matched = not mismatches

    return {
        "id": case.case_id,
        "split": case.split,
        "source": case.source,
        "scenario": case.scenario,
        "inputs": case.inputs,
        "notes": case.notes,
        "expected": case.expected,
        "actual": actual,
        "checks": checks,
        "matched": matched,
        "mismatches": mismatches,
        "error": error,
    }


def _actual_from_report(report: dict[str, Any]) -> dict[str, Any]:
    hypothesis = report["hypothesis"]
    facts = {
        fact: {
            "status": hypothesis[fact]["status"],
            "value": hypothesis[fact].get("value"),
        }
        for fact in _FACT_NAMES
        if fact in hypothesis
    }
    return {
        "analysis_status": report["analysis_status"],
        "extraction_status": report["extraction"]["status"],
        "warning_codes": sorted(
            warning["code"] for warning in report["extraction"]["warnings"]
        ),
        "facts": facts,
        "runnable": hypothesis["scenario_readiness"]["runnable"],
        "executed": report["execution"]["occurred"],
        "verdict": report["verdict"],
        "side_effects": report["execution"]["observed_side_effects"],
    }


def _compare_case(
    expected: dict[str, Any], actual: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    mismatches: list[str] = []
    extraction_ok = actual["extraction_status"] == expected["extraction_status"]
    if not extraction_ok:
        mismatches.append("extraction_status")
    warning_ok = set(actual["warning_codes"]) == set(expected["warning_codes"])
    if not warning_ok:
        mismatches.append("warning_codes")

    fact_checks: dict[str, bool] = {}
    for fact, label in expected["facts"].items():
        actual_fact = actual["facts"].get(fact)
        if actual_fact is None:
            fact_checks[fact] = False
        elif label["status"] == "known":
            fact_checks[fact] = (
                actual_fact["status"] == "known"
                and actual_fact.get("value") == label.get("value")
            )
        else:
            fact_checks[fact] = actual_fact["status"] == label["status"]
        if not fact_checks[fact]:
            mismatches.append(f"fact.{fact}")

    runnable_ok = actual["runnable"] == expected["runnable"]
    if not runnable_ok:
        mismatches.append("runnable")
    execution_ok = actual["executed"] == expected["execution_expected"]
    if not execution_ok:
        mismatches.append("execution")
    verdict_ok = actual["verdict"] == expected["verdict"]
    if not verdict_ok:
        mismatches.append("verdict")
    side_effect_ok = actual["side_effects"] == expected["side_effects"]
    if not side_effect_ok:
        mismatches.append("side_effects")

    checks = {
        "extraction_status": extraction_ok,
        "warning_codes": warning_ok,
        "facts": fact_checks,
        "runnable": runnable_ok,
        "execution": execution_ok,
        "verdict": verdict_ok,
        "side_effects": side_effect_ok,
    }
    return checks, mismatches


def _metric(correct: int, total: int) -> dict[str, Any]:
    return {
        "correct": correct,
        "total": total,
        "rate": round(correct / total, 4) if total else None,
    }


def _rate(metric: dict[str, Any]) -> str:
    rate = metric.get("rate")
    if rate is None:
        return "n/a (0 cases)"
    return f"{metric['correct']}/{metric['total']} ({rate * 100:.1f}%)"


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkDatasetError(f"{label} must be a non-empty string")
    return value