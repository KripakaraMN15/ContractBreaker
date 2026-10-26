"""Versioned JSON report schema and human-readable rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .events import Event
from .extraction import ExtractionResult
from .hypothesis import ClientHypothesis
from .verifier import Report, Verdict
from .version import __version__

SCHEMA_VERSION = "1.0"

EXIT_NOT_REPRODUCED = 0
EXIT_CONFIRMED = 1
EXIT_INCONCLUSIVE = 2
EXIT_INVALID_ARGS = 3
EXIT_UNSUPPORTED_INPUT = 4

_BASE_LIMITATIONS = (
    "The analyzed client is executed in a subprocess against an in-process "
    "WSGI test service; this is isolation, not a hardened security sandbox.",
    "All requests traffic is routed to the test service and socket access is "
    "blocked; direct network clients are unsupported.",
    "Fault injection is simulated in-process after server execution, not at "
    "the network layer.",
    "Static extraction and runtime execution support only the documented "
    "module-level requests client patterns.",
    "Static retry and timeout values are recorded as expectations; the analyzed "
    "client's own code controls actual attempts and request timeouts.",
)


@dataclass(frozen=True)
class ScenarioReadiness:
    """Scenario-specific readiness derived from extracted facts."""

    scenario: str | None
    runnable: bool
    blockers: tuple[str, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioExecution:
    """A completed scenario run plus the settings that produced it."""

    scenario: str
    runner: str
    settings: dict[str, Any]
    provenance: dict[str, str]
    report: Report


def build_report(
    *,
    source_file: str,
    requested_scenario: str,
    extraction: ExtractionResult,
    hypothesis: ClientHypothesis,
    readiness: ScenarioReadiness,
    execution: ScenarioExecution | None,
    analysis_status: str,
    limitations: Iterable[str] = (),
    llm_fallback: dict[str, Any] | None = None,
    fact_provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the stable JSON-native report dictionary."""

    selected_scenario = (
        execution.scenario if execution is not None else readiness.scenario
    )
    extraction_dict = _jsonable(extraction)
    extraction_dict["provenance"] = "static_extraction"

    hypothesis_dict = _jsonable(hypothesis)
    hypothesis_dict["provenance"] = "derived_from_extraction"
    hypothesis_dict["lost_response_ready"] = hypothesis.scenario_ready
    hypothesis_dict["scenario_readiness"] = {
        "scenario": readiness.scenario,
        "runnable": readiness.runnable,
        "blockers": list(readiness.blockers),
        "unknowns": list(readiness.unknowns),
    }
    hypothesis_dict["fact_provenance"] = (
        dict(fact_provenance)
        if fact_provenance is not None
        else {
            "method": "derived_from_extraction",
            "endpoint": "derived_from_extraction",
            "timeout": "derived_from_extraction",
            "max_attempts": "derived_from_extraction",
            "retry_count": "derived_from_extraction",
            "retry_triggers": "derived_from_extraction",
            "idempotency_key_present": "derived_from_extraction",
            "idempotency_key_reused": "derived_from_extraction",
        }
    )
    hypothesis_dict["llm_fallback"] = (
        llm_fallback if llm_fallback is not None else _disabled_llm_fallback()
    )

    if execution is None:
        reason = (
            "; ".join(readiness.blockers)
            if readiness.blockers
            else "scenario was not executed"
        )
        scenario_configuration = {
            "executed": False,
            "scenario": readiness.scenario,
            "runner": None,
            "settings": {},
            "provenance": {},
            "reason": reason,
        }
        execution_dict = {
            "occurred": False,
            "provenance": "not_executed",
            "runner": None,
            "events": [],
            "attempt_ids": [],
            "operation_ids": [],
            "side_effect_ids": [],
            "observed_side_effects": None,
        }
        verdict = Verdict.INCONCLUSIVE.value
        verdict_reason = reason
        verdict_provenance = "not_executed"
    else:
        scenario_configuration = {
            "executed": True,
            "scenario": execution.scenario,
            "runner": execution.runner,
            "settings": _jsonable(execution.settings),
            "provenance": dict(execution.provenance),
            "reason": None,
        }
        events = [_event_dict(event) for event in execution.report.events]
        execution_dict = {
            "occurred": True,
            "provenance": "runtime_observation",
            "runner": execution.runner,
            "events": events,
            "attempt_ids": _unique(
                event["attempt_id"]
                for event in events
                if event["attempt_id"] is not None
            ),
            "operation_ids": _unique(
                event["operation_id"]
                for event in events
                if event["operation_id"] is not None
            ),
            "side_effect_ids": [
                event["data"]["side_effect_id"]
                for event in events
                if event["kind"] == "side_effect_committed"
                and _has_text(event["data"].get("side_effect_id"))
            ],
            "observed_side_effects": execution.report.side_effects,
        }
        verdict = execution.report.verdict.value
        verdict_reason = execution.report.reason
        verdict_provenance = "runtime_verifier"

    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "analysis_status": analysis_status,
        "scenario": requested_scenario,
        "selected_scenario": selected_scenario,
        "source_file": source_file,
        "extraction": extraction_dict,
        "hypothesis": hypothesis_dict,
        "scenario_configuration": scenario_configuration,
        "execution": execution_dict,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "verdict_provenance": verdict_provenance,
        "limitations": _dedupe((*_BASE_LIMITATIONS, *limitations)),
    }


def report_to_json(report: dict[str, Any]) -> str:
    """Serialize a report deterministically without custom objects."""

    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def write_report(report: dict[str, Any], path: str | Path) -> None:
    """Write the JSON report to a UTF-8 file."""

    Path(path).write_text(report_to_json(report), encoding="utf-8")


def exit_code_for_report(report: dict[str, Any]) -> int:
    """Map a report to the documented CLI process exit code."""

    if report.get("analysis_status") in {"unsupported_input", "invalid_input"}:
        return EXIT_UNSUPPORTED_INPUT
    verdict = report.get("verdict")
    if verdict == Verdict.CONFIRMED.value:
        return EXIT_CONFIRMED
    if verdict == Verdict.NOT_REPRODUCED.value:
        return EXIT_NOT_REPRODUCED
    if verdict == Verdict.INCONCLUSIVE.value:
        return EXIT_INCONCLUSIVE
    return EXIT_UNSUPPORTED_INPUT


def render_text(report: dict[str, Any]) -> str:
    """Render a concise developer-facing report without decorative output."""

    lines: list[str] = []
    lines.append("ContractBreaker analysis")
    lines.append(f"Source file: {report['source_file']}")
    lines.append(f"Requested scenario: {report['scenario']}")
    lines.append(f"Selected scenario: {report['selected_scenario'] or 'none'}")
    lines.append(f"Analysis status: {report['analysis_status']}")
    lines.append("")

    extraction = report["extraction"]
    lines.append(f"Extraction: {extraction['status']}")
    calls = extraction.get("calls", [])
    if not calls:
        lines.append("  Calls: none recognized")
    for call in calls:
        location = call["location"]
        lines.append(
            f"  Call at {location['line']}:{location['column']}: "
            f"{_fact(call['method'])} {_fact(call['endpoint'])}"
        )
        lines.append(f"    timeout: {_fact(call['timeout'])}")
        lines.append(f"    idempotency key: {_idempotency_display(call)}")
        if call.get("headers"):
            lines.append(
                "    headers: "
                + ", ".join(
                    str(header["name"].get("value") or header["name"]["status"])
                    for header in call["headers"]
                )
            )
    for loop in extraction.get("retry_loops", []):
        location = loop["location"]
        lines.append(
            f"  Retry loop at {location['line']}:{location['column']}: "
            f"max_attempts={_fact(loop['max_attempts'])}, "
            f"retry_count={_fact(loop['retry_count'])}, "
            f"triggers={_fact(loop['triggers'])}"
        )
    for warning in extraction.get("warnings", []):
        location = warning.get("location")
        where = (
            f" at {location['line']}:{location['column']}" if location else ""
        )
        lines.append(f"  Warning [{warning['code']}]{where}: {warning['message']}")
    lines.append("")

    hypothesis = report["hypothesis"]
    readiness = hypothesis["scenario_readiness"]
    lines.append(f"Hypothesis: provenance={hypothesis['provenance']}")
    for key in (
        "method",
        "endpoint",
        "timeout",
        "retry_count",
        "retry_triggers",
        "idempotency_key_present",
        "idempotency_key_reused",
    ):
        lines.append(f"  {key}: {_fact(hypothesis[key])}")
    lines.append(
        f"  runnable for {readiness['scenario'] or 'the requested scenario'}: "
        f"{'yes' if readiness['runnable'] else 'no'}"
    )
    for blocker in readiness["blockers"]:
        lines.append(f"  blocker: {blocker}")
    for unknown in hypothesis.get("unknowns", []):
        lines.append(f"  unknown: {unknown}")
    for warning in hypothesis.get("warnings", []):
        lines.append(f"  warning: {warning}")
    llm = hypothesis.get("llm_fallback") or {}
    lines.append(
        f"  LLM fallback: {llm.get('outcome', 'disabled')}"
        + (
            f" (model={llm.get('model')}, api_called={llm.get('api_called')})"
            if llm.get("enabled")
            else ""
        )
    )
    if llm.get("enabled"):
        if llm.get("error"):
            lines.append(f"    error: {llm['error']}")
        lines.append(
            "    unresolved="
            f"{len(llm.get('unresolved_facts', []))}, "
            f"proposed={len(llm.get('proposed_facts', []))}, "
            f"accepted={len(llm.get('accepted_facts', []))}, "
            f"rejected={len(llm.get('rejected_facts', []))}, "
            f"conflicts={len(llm.get('conflicts', []))}"
        )
        if llm.get("runnability_before") is not None:
            lines.append(
                "    runnability: "
                f"{llm.get('runnability_before')} -> "
                f"{llm.get('runnability_after')}"
            )
        if llm.get("usage"):
            lines.append(f"    usage: {llm['usage']}")
        if llm.get("estimated_cost_usd") is not None:
            lines.append(
                f"    estimated_cost_usd: {llm['estimated_cost_usd']}"
            )
        for fact in llm.get("accepted_facts", []):
            lines.append(
                "    accepted: "
                f"{fact.get('target_id')}.{fact.get('fact_type')}"
                f"={fact.get('value')}"
            )
        for fact in llm.get("rejected_facts", []):
            errors = "; ".join(fact.get("errors", []))
            lines.append(
                "    rejected: "
                f"{fact.get('target_id')}.{fact.get('fact_type')}"
                + (f" ({errors})" if errors else "")
            )
        for conflict in llm.get("conflicts", []):
            lines.append(
                "    conflict: "
                f"{conflict.get('kind')} "
                f"{conflict.get('target_id')}.{conflict.get('fact_type')}"
            )
    lines.append("")

    configuration = report["scenario_configuration"]
    lines.append("Scenario configuration")
    if configuration["executed"]:
        lines.append(f"  runner: {configuration['runner']}")
        provenance = configuration.get("provenance", {})
        for key, value in configuration.get("settings", {}).items():
            lines.append(
                f"  {key}: {json.dumps(value, sort_keys=True)} "
                f"[{provenance.get(key, 'unspecified')}]"
            )
    else:
        lines.append(f"  not executed: {configuration['reason']}")
    lines.append("")

    execution = report["execution"]
    lines.append("Runtime evidence")
    if not execution["occurred"]:
        lines.append("  none; no runtime scenario was executed")
    else:
        for event in execution["events"]:
            details = []
            if event["attempt_id"] is not None:
                details.append(f"attempt={event['attempt_id']}")
            if event["operation_id"] is not None:
                details.append(f"operation={event['operation_id']}")
            for key in (
                "status_code",
                "side_effect_id",
                "retry_statuses",
                "max_retries",
                "outcome",
            ):
                if key in event["data"]:
                    details.append(f"{key}={event['data'][key]}")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"  {event['seq']:>3} {event['kind']}{suffix}")
        lines.append(
            f"  observed side effects: {execution['observed_side_effects']}"
        )
    lines.append("")

    lines.append(f"Verdict: {str(report['verdict']).upper()}")
    lines.append(f"Verdict provenance: {report['verdict_provenance']}")
    lines.append(f"Reason: {report['verdict_reason']}")
    lines.append(f"Outcome: {_outcome(report)}")
    lines.append("")
    lines.append("Limitations")
    for limitation in report.get("limitations", []):
        lines.append(f"  - {limitation}")
    return "\n".join(lines)


def _outcome(report: dict[str, Any]) -> str:
    status = report["analysis_status"]
    if status == "unsupported_input":
        return "unsupported or invalid input; no scenario was executed."
    if status == "not_runnable":
        return (
            "potential conflict not evaluated because the scenario is not runnable; "
            "no safety claim."
        )
    verdict = report["verdict"]
    if verdict == Verdict.CONFIRMED.value:
        return "failure reproduced under the configured runtime scenario."
    if verdict == Verdict.NOT_REPRODUCED.value:
        return (
            "no duplicate reproduced in this configured scenario; "
            "this is not proof of production safety."
        )
    return "analysis inconclusive; do not treat this as a safe result."


def _idempotency_display(call: dict[str, Any]) -> str:
    fact = call["idempotency_key"]
    if fact["status"] == "known" and fact.get("value"):
        info = fact["value"]
        return (
            f"present; reuse_across_attempts="
            f"{_fact(info['reused_across_attempts'])}"
        )
    return _fact(fact)


def _fact(fact: dict[str, Any]) -> str:
    status = fact["status"]
    value = fact.get("value")
    reason = fact.get("reason")
    if status == "known":
        if isinstance(value, (dict, list, tuple)):
            return f"known({json.dumps(value, sort_keys=True)})"
        return f"known({value!r})"
    if reason:
        return f"{status} ({reason})"
    return str(status)


def _disabled_llm_fallback() -> dict[str, Any]:
    return {
        "enabled": False,
        "outcome": "disabled",
        "model": None,
        "api_called": False,
        "error": None,
        "unresolved_facts": [],
        "proposed_facts": [],
        "accepted_facts": [],
        "rejected_facts": [],
        "conflicts": [],
        "usage": None,
        "estimated_cost_usd": None,
        "runnability_before": None,
        "runnability_after": None,
        "runnability_changed": None,
        "limitations": [],
    }


def _event_dict(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind.value,
        "attempt_id": event.attempt_id,
        "operation_id": event.operation_id,
        "data": _jsonable(event.data),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    if isinstance(value, Path):
        return str(value)
    return value


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unique(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))