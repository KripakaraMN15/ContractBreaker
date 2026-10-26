"""Evidence-driven workflow orchestration for ContractBreaker.

The workflow composes existing components: the deterministic extractor, the
bounded LLM fallback, the hypothesis builder, runtime-contract readiness, the
scenario runners, and the causal verifiers. It adds request interpretation,
explicit stage results, an approval boundary, and evidence explanations.

The orchestrator never issues a runtime verdict itself. Only the existing
scenario verifier can produce ``confirmed`` or ``not_reproduced``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .explain import SummaryResult, build_deterministic_explanation, summarize_report
from .llm import LLMClient, LLMInvalidOutput, LLMUnavailable
from .pipeline import (
    AnalysisOptions,
    AnalysisUsageError,
    PreparedAnalysis,
    SourceReadError,
    execute_prepared,
    prepare_analysis,
)
from .report import build_analysis_snapshot, build_report
from .version import __version__

WORKFLOW_SCHEMA_VERSION = "1.0"
SUPPORTED_WORKFLOW_SCENARIOS = ("lost-response", "retryable-error")

_SCENARIO_MARKERS: dict[str, tuple[str, ...]] = {
    "lost-response": (
        "lost response",
        "lost-response",
        "dropped response",
        "response dropped",
        "response is dropped",
        "response is lost",
        "timeout",
        "timed out",
        "times out",
        "no response",
    ),
    "retryable-error": (
        "retryable error",
        "retryable-error",
        "retryable status",
        "server error",
        "503",
        "502",
        "500",
        "5xx",
    ),
}
_UNSUPPORTED_MARKERS: dict[str, str] = {
    "circuit breaker": "circuit breakers are outside the supported scenarios",
    "rate limit": "rate-limit handling is outside the supported scenarios",
    "429": "rate-limit handling is outside the supported scenarios",
    "dns": "DNS failures are outside the supported scenarios",
    "websocket": "WebSocket behavior is outside the supported scenarios",
    "grpc": "gRPC behavior is outside the supported scenarios",
    "database": "database transaction behavior is outside the supported scenarios",
    "connection reset": "connection-reset handling is outside the supported scenarios",
}
_MIN_PHRASE_LENGTH = 4

_INTERPRET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scenario", "matched_phrase", "reason"],
    "properties": {
        "scenario": {
            "type": "string",
            "enum": ["lost-response", "retryable-error", "unsupported"],
        },
        "matched_phrase": {"type": "string"},
        "reason": {"type": "string"},
    },
}

_INTERPRET_SYSTEM = (
    "Classify a reliability-testing request into one supported ContractBreaker "
    "scenario: lost-response (a response is lost after a server side effect, so "
    "the client retries) or retryable-error (the server commits and returns a "
    "retryable HTTP error, and the client retries). Use unsupported when the "
    "request is outside those scenarios. Cite a phrase copied verbatim from the "
    "request as matched_phrase. Do not invent requirements. Keep the reason "
    "concise and do not provide chain-of-thought."
)


class StageOutcome(str, Enum):
    """Outcome of a single workflow stage."""

    SUCCESS = "success"
    BLOCKED = "blocked"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ApprovalState(str, Enum):
    """Whether execution has been explicitly approved."""

    PENDING = "pending"
    APPROVED = "approved"


@dataclass(frozen=True)
class StageResult:
    """A typed stage outcome with explicit blockers and details."""

    stage: str
    outcome: StageOutcome
    summary: str
    blockers: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "blockers": list(self.blockers),
            "details": self.details,
        }


@dataclass(frozen=True)
class WorkflowRequest:
    """Raw user request plus explicit structured overrides."""

    request: str
    source: Path | None = None
    scenario: str | None = None
    retry_statuses: tuple[int, ...] | None = None
    server_idempotency: str | None = None
    server_error_status: int | None = None
    execution_timeout: float | None = None
    llm_fallback: bool = False
    llm_client: LLMClient | None = None
    approved: bool = False
    plan_only: bool = False


@dataclass(frozen=True)
class NormalizedRequest:
    """Deterministically or LLM-assisted interpretation of the request."""

    text: str
    source: Path
    scenario: str
    scenario_source: str
    constraints: dict[str, Any]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": str(self.source),
            "scenario": self.scenario,
            "scenario_source": self.scenario_source,
            "constraints": self.constraints,
            "notes": list(self.notes),
        }


@dataclass
class WorkflowState:
    """Accumulated typed state for one workflow run."""

    request: WorkflowRequest
    normalized: NormalizedRequest | None = None
    stages: list[StageResult] = field(default_factory=list)
    analysis_snapshot: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    approval: ApprovalState = ApprovalState.PENDING
    explanation: SummaryResult | None = None
    limitations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outcome: str = "failed"

    @property
    def verdict(self) -> str | None:
        return self.report["verdict"] if self.report is not None else None

    @property
    def verdict_reason(self) -> str | None:
        return self.report["verdict_reason"] if self.report is not None else None

    def add(self, stage: StageResult) -> None:
        self.stages.append(stage)

    def snapshot(self) -> dict[str, Any]:
        """Return the JSON-native workflow document."""

        latest_stage = self.stages[-1] if self.stages else None
        return {
            "workflow_schema_version": WORKFLOW_SCHEMA_VERSION,
            "tool_version": __version__,
            "outcome": self.outcome,
            "request": {
                "text": self.request.request,
                "source": (
                    str(self.normalized.source)
                    if self.normalized is not None
                    else (
                        str(self.request.source)
                        if self.request.source is not None
                        else None
                    )
                ),
                "scenario": (
                    self.normalized.scenario
                    if self.normalized is not None
                    else self.request.scenario
                ),
                "scenario_source": (
                    self.normalized.scenario_source
                    if self.normalized is not None
                    else None
                ),
                "constraints": (
                    self.normalized.constraints
                    if self.normalized is not None
                    else {}
                ),
                "llm_fallback": self.request.llm_fallback,
                "plan_only": self.request.plan_only,
            },
            "approval": self.approval.value,
            "stages": [stage.to_dict() for stage in self.stages],
            "last_stage": latest_stage.stage if latest_stage else None,
            "analysis_snapshot": self.analysis_snapshot,
            "analysis": self.report,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "explanation": (
                self.explanation.text
                if self.explanation is not None
                else (
                    build_deterministic_explanation(self.report)
                    if self.report is not None
                    else None
                )
            ),
            "explanation_meta": (
                self.explanation.to_dict()
                if self.explanation is not None
                else {"outcome": "not_generated", "used_llm": False}
            ),
            "limitations": list(dict.fromkeys(self.limitations)),
            "errors": list(dict.fromkeys(self.errors)),
        }


def run_workflow(request: WorkflowRequest) -> WorkflowState:
    """Interpret the request, plan, gate on approval, then execute if allowed."""

    state = WorkflowState(request=request)

    interpretation = interpret_request(request)
    state.add(interpretation.stage)
    if interpretation.outcome is not StageOutcome.SUCCESS:
        return _finish(state, "blocked")
    normalized = interpretation.normalized
    assert normalized is not None
    state.normalized = normalized

    try:
        prepared = prepare_analysis(_analysis_options(request, normalized))
    except (SourceReadError, AnalysisUsageError) as exc:
        state.errors.append(f"{type(exc).__name__}: {exc}")
        state.add(
            StageResult(
                "source_analysis",
                StageOutcome.FAILED,
                "Source analysis could not be completed.",
                blockers=(str(exc),),
            )
        )
        return _finish(state, "failed")

    analysis_stage = _analysis_stage(prepared)
    state.analysis_snapshot = build_analysis_snapshot(
        extraction=prepared.extraction,
        hypothesis=prepared.hypothesis,
        readiness=prepared.readiness,
        llm_fallback=prepared.llm_diagnostics,
        fact_provenance=prepared.llm_result.fact_provenance,
    )
    state.add(analysis_stage)
    if analysis_stage.outcome is not StageOutcome.SUCCESS:
        state.limitations.extend(prepared.limitations)
        return _finish(state, "blocked")

    readiness_stage = _readiness_stage(prepared)
    state.add(readiness_stage)
    if readiness_stage.outcome is not StageOutcome.SUCCESS:
        state.limitations.extend(prepared.limitations)
        return _finish(state, "blocked")

    if request.plan_only:
        state.add(
            StageResult(
                "approval",
                StageOutcome.BLOCKED,
                "Plan-only mode: execution was not requested.",
                details={"blocked_kind": "plan_only"},
            )
        )
        state.limitations.append(
            "Plan-only mode stops before execution; no runtime evidence exists."
        )
        return _finish(state, "planned")

    if not request.approved:
        state.add(
            StageResult(
                "approval",
                StageOutcome.BLOCKED,
                "Execution requires explicit approval.",
                blockers=(
                    "Re-run with --approve to execute this runnable scenario.",
                ),
                details={"blocked_kind": "approval_required"},
            )
        )
        state.limitations.append(
            "Execution was not approved; no runtime evidence exists."
        )
        return _finish(state, "planned")

    state.approval = ApprovalState.APPROVED
    state.add(
        StageResult(
            "approval",
            StageOutcome.SUCCESS,
            "Execution approved explicitly for this request.",
            details={"approved": True},
        )
    )

    outcome = execute_prepared(prepared)
    report = build_report(
        source_file=str(prepared.path),
        requested_scenario=request.scenario or normalized.scenario,
        extraction=prepared.extraction,
        hypothesis=prepared.hypothesis,
        readiness=outcome.readiness,
        execution=outcome.execution,
        analysis_status=outcome.analysis_status,
        limitations=outcome.limitations,
        llm_fallback=prepared.llm_diagnostics,
        fact_provenance=prepared.llm_result.fact_provenance,
    )
    state.report = report
    state.limitations.extend(outcome.limitations)

    execution_stage = StageResult(
        "execution",
        (
            StageOutcome.SUCCESS
            if outcome.analysis_status == "executed"
            else StageOutcome.FAILED
        ),
        (
            "Executed the analyzed client and applied the causal verifier."
            if outcome.analysis_status == "executed"
            else "Runtime execution failed before a verdict could be verified."
        ),
        blockers=(),
        details={
            "analysis_status": outcome.analysis_status,
            "executed": report["execution"]["occurred"],
            "observed_side_effects": report["execution"][
                "observed_side_effects"
            ],
            "attempt_ids": report["execution"]["attempt_ids"],
            "side_effect_ids": report["execution"]["side_effect_ids"],
            "verdict": report["verdict"],
            "limitations": list(outcome.limitations),
        },
    )
    state.add(execution_stage)

    summary = summarize_report(
        report,
        enabled=request.llm_fallback,
        client=request.llm_client,
    )
    state.explanation = summary
    state.add(
        StageResult(
            "explanation",
            StageOutcome.SUCCESS,
            f"Evidence explanation produced ({summary.outcome}).",
            details=summary.to_dict(),
        )
    )
    if summary.rejection_reason:
        state.limitations.append(
            "LLM summary rejected; deterministic explanation used: "
            + summary.rejection_reason
        )

    if execution_stage.outcome is not StageOutcome.SUCCESS:
        return _finish(state, "failed")
    return _finish(state, "executed")


@dataclass(frozen=True)
class Interpretation:
    """Result of request interpretation, exposing the stage explicitly."""

    stage: StageResult
    normalized: NormalizedRequest | None

    @property
    def outcome(self) -> StageOutcome:
        return self.stage.outcome


def interpret_request(request: WorkflowRequest) -> Interpretation:
    """Resolve scenario, source, and constraints without guessing."""

    text = " ".join(request.request.strip().split())
    lowered = text.lower()
    if not text:
        if request.scenario is None or request.source is None:
            return _blocked(
                "invalid_request",
                "A testing request is required.",
                (
                    "Provide --request, or provide both --scenario and --source.",
                ),
            )
        # Structured input: scenario and source supplied without free text.
        return _structured_interpretation(
            request,
            notes=("Structured input: no free-text request was supplied.",),
        )

    for marker, reason in _UNSUPPORTED_MARKERS.items():
        if marker in lowered:
            return _blocked(
                "unsupported_request",
                f"Unsupported request topic: {marker}.",
                (reason, "Only lost-response and retryable-error are supported."),
            )

    notes: list[str] = []
    scenario_source: str
    scenario: str | None
    if request.scenario is not None:
        if request.scenario not in SUPPORTED_WORKFLOW_SCENARIOS:
            return _blocked(
                "invalid_request",
                f"Unsupported scenario: {request.scenario}.",
                (
                    "Choose lost-response or retryable-error.",
                ),
            )
        scenario = request.scenario
        scenario_source = "user_structured"
        notes.append("Scenario supplied explicitly by the user.")
    else:
        matches = [
            candidate
            for candidate, markers in _SCENARIO_MARKERS.items()
            if any(_marker_present(marker, lowered) for marker in markers)
        ]
        if len(matches) > 1:
            return _blocked(
                "invalid_request",
                "The request matches both scenarios.",
                (
                    "Specify --scenario lost-response or --scenario retryable-error.",
                ),
            )
        if len(matches) == 1:
            scenario = matches[0]
            scenario_source = "request_text"
        else:
            proposal = _llm_interpret(request, text)
            if proposal is None:
                return _blocked(
                    "invalid_request",
                    "The scenario could not be determined without guessing.",
                    (
                        "Name a supported scenario or pass --scenario.",
                    ),
                )
            scenario, phrase, reason = proposal
            scenario_source = "llm_proposal"
            notes.append(
                f"Scenario proposed by the LLM from the request phrase "
                f"{phrase!r}: {reason}"
            )

    source = _resolve_source(request, text)
    if source is None:
        return _blocked(
            "invalid_request",
            "No target source file was identified.",
            ("Pass --source path/to/client.py.",),
        )
    if not source.is_file():
        return Interpretation(
            stage=StageResult(
                "request_interpretation",
                StageOutcome.BLOCKED,
                f"Source file not found: {source}.",
                blockers=(f"Source file not found: {source}",),
                details={"blocked_kind": "unsupported_request"},
            ),
            normalized=None,
        )

    constraints = _resolve_constraints(request, scenario)
    if constraints is None:
        return _blocked(
            "invalid_request",
            "Invalid constraint for the selected scenario.",
            (
                "Retry statuses are only valid for retryable-error.",
            ),
        )

    normalized = NormalizedRequest(
        text=text,
        source=source,
        scenario=scenario,
        scenario_source=scenario_source,
        constraints=constraints,
        notes=tuple(notes),
    )
    stage = StageResult(
        "request_interpretation",
        StageOutcome.SUCCESS,
        f"Interpreted request as {scenario} for {source.name}.",
        details={
            "scenario": scenario,
            "scenario_source": scenario_source,
            "source": str(source),
            "constraints": constraints,
            "notes": list(notes),
        },
    )
    return Interpretation(stage=stage, normalized=normalized)


def _marker_present(marker: str, lowered_text: str) -> bool:
    if marker.isdigit():
        return _numeric_marker(marker).search(lowered_text) is not None
    return marker in lowered_text


def _numeric_marker(marker: str):
    return re.compile(rf"(?<![\w.]){marker}(?![\w.])")


def _structured_interpretation(
    request: WorkflowRequest, *, notes: tuple[str, ...]
) -> Interpretation:
    scenario = request.scenario
    source = request.source
    if scenario not in SUPPORTED_WORKFLOW_SCENARIOS or source is None:
        return _blocked(
            "invalid_request",
            "Structured input requires a supported scenario and a source file.",
            ("Provide --scenario lost-response|retryable-error and --source.",),
        )
    if not source.is_file():
        return Interpretation(
            stage=StageResult(
                "request_interpretation",
                StageOutcome.BLOCKED,
                f"Source file not found: {source}.",
                blockers=(f"Source file not found: {source}",),
                details={"blocked_kind": "unsupported_request"},
            ),
            normalized=None,
        )
    constraints = _resolve_constraints(request, scenario)
    if constraints is None:
        return _blocked(
            "invalid_request",
            "Invalid constraint for the selected scenario.",
            ("Retry statuses are only valid for retryable-error.",),
        )
    normalized = NormalizedRequest(
        text=request.request.strip(),
        source=source,
        scenario=scenario,
        scenario_source="user_structured",
        constraints=constraints,
        notes=notes,
    )
    return Interpretation(
        stage=StageResult(
            "request_interpretation",
            StageOutcome.SUCCESS,
            f"Interpreted structured input as {scenario} for {source.name}.",
            details={
                "scenario": scenario,
                "scenario_source": "user_structured",
                "source": str(source),
                "constraints": constraints,
                "notes": list(notes),
            },
        ),
        normalized=normalized,
    )


def _blocked(
    kind: str, summary: str, blockers: tuple[str, ...]
) -> Interpretation:
    return Interpretation(
        stage=StageResult(
            "request_interpretation",
            StageOutcome.BLOCKED,
            summary,
            blockers=blockers,
            details={"blocked_kind": kind},
        ),
        normalized=None,
    )


def _llm_interpret(
    request: WorkflowRequest, text: str
) -> tuple[str, str, str] | None:
    if not request.llm_fallback:
        return None
    try:
        client = request.llm_client
        if client is None:
            from .llm import create_default_llm_client

            client = create_default_llm_client()
        completion = client.complete_json(
            system=_INTERPRET_SYSTEM,
            payload={
                "request": text,
                "allowed_scenarios": list(SUPPORTED_WORKFLOW_SCENARIOS),
            },
            schema=_INTERPRET_SCHEMA,
            schema_name="contractbreaker_request_interpretation",
        )
    except (LLMUnavailable, LLMInvalidOutput):
        return None
    except Exception:
        return None

    data = completion.data
    scenario = data.get("scenario")
    phrase = data.get("matched_phrase")
    reason = data.get("reason")
    if scenario not in SUPPORTED_WORKFLOW_SCENARIOS:
        return None
    if not isinstance(phrase, str) or len(phrase.strip()) < _MIN_PHRASE_LENGTH:
        return None
    if phrase.strip().lower() not in text.lower():
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return scenario, phrase.strip(), reason.strip()


def _resolve_source(request: WorkflowRequest, text: str) -> Path | None:
    if request.source is not None:
        return Path(request.source)
    return _source_from_text(text)


def _source_from_text(text: str) -> Path | None:
    """Find the longest existing ``*.py`` path mentioned in the request.

    Paths may contain spaces, so candidate spans are expanded to the left and
    the longest span that exists on disk wins.
    """

    best: Path | None = None
    index = text.find(".py")
    while index != -1:
        end = index + len(".py")
        for start in range(0, end + 1):
            candidate = text[start:end].strip().strip("\"'`,;:")
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file() and (best is None or len(candidate) > len(str(best))):
                best = path
        index = text.find(".py", index + 1)
    return best


def _resolve_constraints(
    request: WorkflowRequest, scenario: str
) -> dict[str, Any] | None:
    constraints: dict[str, Any] = {}
    if request.server_idempotency is not None:
        constraints["server_idempotency"] = request.server_idempotency
    if request.execution_timeout is not None:
        constraints["execution_timeout"] = request.execution_timeout
    if request.retry_statuses is not None:
        if scenario != "retryable-error":
            return None
        constraints["retry_statuses"] = list(request.retry_statuses)
    if request.server_error_status is not None:
        if scenario != "retryable-error":
            return None
        constraints["server_error_status"] = request.server_error_status
    return constraints


def _analysis_options(
    request: WorkflowRequest, normalized: NormalizedRequest
) -> AnalysisOptions:
    constraints = normalized.constraints
    retry_statuses = constraints.get("retry_statuses")
    return AnalysisOptions(
        source=normalized.source,
        scenario=normalized.scenario,
        server_idempotency=constraints.get("server_idempotency", "auto"),
        retry_statuses=(
            tuple(retry_statuses) if retry_statuses is not None else None
        ),
        server_error_status=constraints.get("server_error_status"),
        execution_timeout=constraints.get("execution_timeout"),
        llm_fallback=request.llm_fallback,
        llm_client=request.llm_client,
    )


def _analysis_stage(prepared: PreparedAnalysis) -> StageResult:
    extraction = prepared.extraction
    llm = prepared.llm_diagnostics
    details = {
        "extraction_status": extraction.status.value,
        "http_call_count": len(extraction.calls),
        "retry_loop_count": len(extraction.retry_loops),
        "warning_codes": [warning.code for warning in extraction.warnings],
        "fact_provenance": prepared.llm_result.fact_provenance,
        "llm_fallback": {
            "enabled": llm.get("enabled"),
            "outcome": llm.get("outcome"),
            "accepted_facts": len(llm.get("accepted_facts", [])),
            "rejected_facts": len(llm.get("rejected_facts", [])),
            "conflicts": len(llm.get("conflicts", [])),
        },
    }
    blockers: list[str] = []
    notes: list[str] = []
    if extraction.status.value in {
        "unsupported",
        "no_http_calls",
        "parse_error",
    }:
        blockers.append(f"extraction status is {extraction.status.value}")
        blockers.extend(warning.code for warning in extraction.warnings)
        return StageResult(
            "source_analysis",
            StageOutcome.BLOCKED,
            "The source could not be analyzed into supported client facts.",
            blockers=tuple(dict.fromkeys(blockers)),
            details={**details, "blocked_kind": "unsupported_request"},
        )
    if llm.get("conflicts"):
        notes.append(
            f"{len(llm['conflicts'])} conflicting fact proposal(s) were rejected"
        )
    if llm.get("outcome") in {"unavailable", "invalid_output"}:
        notes.append(
            "LLM fallback did not complete; deterministic facts were preserved"
        )
    return StageResult(
        "source_analysis",
        StageOutcome.SUCCESS,
        (
            f"Extracted {len(extraction.calls)} HTTP call(s) and "
            f"{len(extraction.retry_loops)} retry loop(s) "
            f"(status: {extraction.status.value})."
        ),
        blockers=(),
        details={**details, "notes": notes},
    )


def _readiness_stage(prepared: PreparedAnalysis) -> StageResult:
    readiness = prepared.readiness
    details = {
        "scenario": readiness.scenario,
        "runnable": readiness.runnable,
        "blockers": list(readiness.blockers),
        "unknowns": list(readiness.unknowns),
        "runtime_contract": {
            "supported": prepared.contract.supported,
            "blockers": list(prepared.contract.blockers),
        },
    }
    if not readiness.runnable:
        blockers = readiness.blockers or (
            "hypothesis is not runnable for the requested scenario",
        )
        return StageResult(
            "readiness_validation",
            StageOutcome.BLOCKED,
            "The client is not runnable for the requested scenario.",
            blockers=blockers,
            details={**details, "blocked_kind": "not_runnable"},
        )
    return StageResult(
        "readiness_validation",
        StageOutcome.SUCCESS,
        "All scenario and runtime-contract checks passed.",
        details=details,
    )


def _finish(state: WorkflowState, outcome: str) -> WorkflowState:
    state.outcome = outcome
    return state


def workflow_exit_code(state: WorkflowState) -> int:
    """Map workflow state to the documented exit codes."""

    if state.outcome == "executed" and state.verdict is not None:
        return {
            "confirmed": 1,
            "not_reproduced": 0,
            "inconclusive": 2,
        }.get(state.verdict, 2)
    if state.outcome == "failed":
        return 2
    for stage in state.stages:
        kind = stage.details.get("blocked_kind")
        if stage.outcome is StageOutcome.BLOCKED and kind is not None:
            if kind == "invalid_request":
                return 3
            if kind == "unsupported_request":
                return 4
            return 2
    return 2


def render_workflow_text(state: WorkflowState) -> str:
    """Render the workflow stages and evidence explanation for a terminal."""

    document = state.snapshot()
    lines = [
        "ContractBreaker workflow",
        f"Request: {document['request']['text']}",
        f"Scenario: {document['request']['scenario'] or 'unresolved'} "
        f"({document['request']['scenario_source'] or 'n/a'})",
        f"Source: {document['request']['source'] or 'unresolved'}",
        f"Outcome: {document['outcome']}",
        f"Approval: {document['approval']}",
        "",
        "Stages:",
    ]
    for stage in document["stages"]:
        lines.append(f"  [{stage['outcome']}] {stage['stage']}: {stage['summary']}")
        for blocker in stage["blockers"]:
            lines.append(f"    blocker: {blocker}")

    snapshot = document["analysis_snapshot"]
    if snapshot is not None:
        readiness = snapshot["hypothesis"]["scenario_readiness"]
        lines.append("")
        lines.append(
            "Readiness: "
            f"runnable={readiness['runnable']} "
            f"scenario={readiness['scenario']}"
        )
        for blocker in readiness["blockers"]:
            lines.append(f"  blocker: {blocker}")

    if document["verdict"] is not None:
        lines.append("")
        lines.append(f"Verdict: {str(document['verdict']).upper()}")
        lines.append(f"Reason: {document['verdict_reason']}")

    if document["explanation"]:
        lines.append("")
        lines.append("Explanation:")
        lines.append(document["explanation"])

    if document["limitations"]:
        lines.append("")
        lines.append("Limitations:")
        for limitation in document["limitations"]:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)