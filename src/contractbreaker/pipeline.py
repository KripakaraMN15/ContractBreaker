"""End-to-end analysis pipeline used by the CLI.

The pipeline connects extraction, hypothesis generation, runtime execution of
the analyzed client, and causal verification. It never substitutes the bundled
reference client for an analyzed source.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .ast_extractor import extract_from_source
from .events import EventKind
from .extraction import ExtractionResult, ExtractionStatus, FactStatus
from .hypothesis import (
    ClientHypothesis,
    HypothesisNotRunnable,
    hypothesize_from_extraction,
)
from .idempotency import check_idempotency_key_stability
from .llm import LLMClient
from .llm_fallback import LLMFallbackResult, run_llm_fallback
from .report import ScenarioExecution, ScenarioReadiness, build_report
from .retry_budget import check_retry_budget
from .retryable_error_verifier import verify_retryable_error_duplicate
from .runtime import (
    RuntimeExecutionError,
    RuntimeExecutionTimeout,
    run_analyzed_client,
)
from .runtime_contract import RuntimeContract, check_runtime_contract
from .timeout_contract import DEFAULT_TIMEOUT_INVARIANT, TIMEOUT_INVARIANTS
from .timeout_verifier import verify_client_timeout
from .verifier import verify_duplicate_side_effects

SUPPORTED_SCENARIOS = (
    "auto",
    "lost-response",
    "retryable-error",
    "client-timeout",
)
SUPPORTED_SERVER_IDEMPOTENCY = ("auto", "on", "off")
SUPPORTED_TIMEOUT_MODES = (
    "server_first",
    "client_first_then_commit",
    "client_first_no_commit",
)
DEFAULT_TIMEOUT_MODE = "client_first_then_commit"
_UNSUPPORTED_EXTRACTION = {
    ExtractionStatus.PARSE_ERROR,
    ExtractionStatus.NO_HTTP_CALLS,
    ExtractionStatus.UNSUPPORTED,
}
_DEFAULT_EXECUTION_TIMEOUT = 10.0


class AnalysisUsageError(ValueError):
    """Raised for invalid CLI/pipeline combinations."""


class SourceReadError(OSError):
    """Raised when the source file can not be read."""


@dataclass(frozen=True)
class AnalysisOptions:
    """Effective options for one analysis run."""

    source: Path
    scenario: str = "auto"
    server_idempotency: str = "auto"
    retry_statuses: tuple[int, ...] | None = None
    server_error_status: int | None = None
    execution_timeout: float | None = None
    llm_fallback: bool = False
    llm_client: LLMClient | None = None
    timeout_mode: str | None = None
    timeout_invariant: str | None = None


@dataclass(frozen=True)
class AnalysisSettings:
    """Runtime settings resolved from CLI input and scenario defaults."""

    retry_statuses: tuple[int, ...] | None
    retry_provenance: str | None
    server_error_status: int | None
    server_error_provenance: str | None
    execution_timeout: float
    execution_timeout_provenance: str
    timeout_mode: str | None = None
    timeout_mode_provenance: str | None = None
    timeout_invariant: str | None = None
    timeout_invariant_provenance: str | None = None


@dataclass(frozen=True)
class PreparedAnalysis:
    """Everything known before any runtime execution.

    Preparing is read-only: it extracts facts, applies the optional LLM
    fallback, and resolves readiness, but never runs the analyzed client.
    """

    options: AnalysisOptions
    path: Path
    extraction: ExtractionResult
    hypothesis_before: ClientHypothesis
    hypothesis: ClientHypothesis
    llm_result: LLMFallbackResult
    llm_diagnostics: dict[str, Any]
    contract: RuntimeContract
    selected: str | None
    readiness: ScenarioReadiness
    ready: bool
    pre_status: str
    unsupported_extraction: bool
    settings: AnalysisSettings
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionOutcome:
    """A runtime attempt plus the report status it produced."""

    execution: ScenarioExecution | None
    readiness: ScenarioReadiness
    analysis_status: str
    limitations: tuple[str, ...]
    failure: dict[str, Any] | None = None


def prepare_analysis(options: AnalysisOptions) -> PreparedAnalysis:
    """Run extraction, fallback, readiness, and contract checks only."""

    _validate_options(options)
    path = Path(options.source)
    if not path.is_file():
        raise SourceReadError(f"source file not found: {path}")
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceReadError(f"source file could not be read: {path}") from exc

    extraction = extract_from_source(source, filename=str(path))
    hypothesis_before = hypothesize_from_extraction(extraction)
    llm_result: LLMFallbackResult = run_llm_fallback(
        source=source,
        extraction=extraction,
        hypothesis=hypothesis_before,
        enabled=options.llm_fallback,
        client=options.llm_client,
    )
    hypothesis = llm_result.hypothesis
    contract = check_runtime_contract(source, extraction)
    unsupported_extraction = extraction.status in _UNSUPPORTED_EXTRACTION
    settings = _resolve_settings(options)

    selected, readiness = _select_scenario(
        options, hypothesis, unsupported_extraction, contract
    )
    if unsupported_extraction:
        readiness = ScenarioReadiness(
            selected,
            False,
            _dedupe(
                (
                    f"extraction status is {extraction.status.value}",
                    *readiness.blockers,
                )
            ),
            readiness.unknowns,
        )
        pre_status = "unsupported_input"
    elif not readiness.runnable:
        pre_status = "not_runnable"
    else:
        pre_status = "executed"

    llm_diagnostics = llm_result.diagnostics
    if options.llm_fallback:
        before_readiness = (
            None
            if selected is None or unsupported_extraction
            else _readiness_for_scenario(selected, hypothesis_before, contract)
        )
        before_runnable = bool(before_readiness and before_readiness.runnable)
        after_runnable = bool(
            selected is not None
            and not unsupported_extraction
            and readiness.runnable
        )
        llm_diagnostics["runnability_before"] = before_runnable
        llm_diagnostics["runnability_after"] = after_runnable
        llm_diagnostics["runnability_changed"] = (
            before_runnable != after_runnable
        )
        if before_runnable != after_runnable:
            llm_diagnostics["runnability_reason"] = (
                "scenario readiness changed after validated LLM facts were merged"
            )
        else:
            llm_diagnostics["runnability_reason"] = None

    limitations: list[str] = []
    if pre_status == "unsupported_input":
        limitations.append(
            "No runtime scenario was executed because the source is "
            "unsupported or has no recognized requests call."
        )
    elif pre_status == "not_runnable":
        limitations.extend(
            (
                (
                    "No runtime scenario was executed because required facts or "
                    "the runtime execution contract were unresolved."
                ),
                (
                    "The analyzed source was not substituted with the bundled "
                    "reference client."
                ),
            )
        )
        if options.scenario == "auto":
            limitations.append(
                "Automatic selection currently supports only a runnable "
                "lost-response hypothesis; specify a scenario explicitly."
            )

    return PreparedAnalysis(
        options=options,
        path=path,
        extraction=extraction,
        hypothesis_before=hypothesis_before,
        hypothesis=hypothesis,
        llm_result=llm_result,
        llm_diagnostics=llm_diagnostics,
        contract=contract,
        selected=selected,
        readiness=readiness,
        ready=pre_status == "executed",
        pre_status=pre_status,
        unsupported_extraction=unsupported_extraction,
        settings=settings,
        limitations=tuple(limitations),
    )


def execute_prepared(prepared: PreparedAnalysis) -> ExecutionOutcome:
    """Execute a prepared scenario only when readiness checks passed."""

    if not prepared.ready:
        return ExecutionOutcome(
            execution=None,
            readiness=prepared.readiness,
            analysis_status=prepared.pre_status,
            limitations=prepared.limitations,
        )

    options = prepared.options
    settings = prepared.settings
    readiness = prepared.readiness
    execution: ScenarioExecution | None = None
    analysis_status = "executed"
    limitations: list[str] = []
    failure_block: dict[str, Any] | None = None

    try:
        if prepared.selected == "lost-response":
            execution, execution_limits = _execute_lost_response(
                prepared.hypothesis,
                options,
                prepared.path,
                settings.execution_timeout,
                settings.execution_timeout_provenance,
            )
        elif prepared.selected == "retryable-error":
            assert settings.retry_statuses is not None
            assert settings.retry_provenance is not None
            assert settings.server_error_status is not None
            assert settings.server_error_provenance is not None
            execution, execution_limits = _execute_retryable_error(
                prepared.hypothesis,
                options,
                prepared.path,
                settings.retry_statuses,
                settings.retry_provenance,
                settings.server_error_status,
                settings.server_error_provenance,
                settings.execution_timeout,
                settings.execution_timeout_provenance,
            )
        elif prepared.selected == "client-timeout":
            execution, execution_limits = _execute_client_timeout(
                prepared.hypothesis,
                options,
                prepared.path,
                settings,
            )
        else:
            raise AnalysisUsageError("no runnable scenario was selected")
        limitations.extend(execution_limits)
    except RuntimeExecutionError as exc:
        failure_message = f"runtime execution failed: {exc}"
        failure_block = {
            "kind": (
                "harness_timeout"
                if isinstance(exc, RuntimeExecutionTimeout)
                else "worker_error"
            ),
            "message": str(exc),
        }
        readiness = ScenarioReadiness(
            prepared.selected,
            False,
            _dedupe((failure_message, *readiness.blockers)),
            prepared.hypothesis.unknowns,
        )
        analysis_status = "not_runnable"
        execution = None
        limitations.append(failure_message)

    if analysis_status == "not_runnable" and not execution:
        limitations.extend(
            (
                (
                    "No runtime scenario was executed because required facts or "
                    "the runtime execution contract were unresolved."
                ),
                (
                    "The analyzed source was not substituted with the bundled "
                    "reference client."
                ),
            )
        )
        if prepared.options.scenario == "auto":
            limitations.append(
                "Automatic selection currently supports only a runnable "
                "lost-response hypothesis; specify a scenario explicitly."
            )

    return ExecutionOutcome(
        execution=execution,
        readiness=readiness,
        analysis_status=analysis_status,
        limitations=tuple(limitations),
        failure=failure_block,
    )


def analyze(options: AnalysisOptions) -> dict[str, Any]:
    """Extract, prepare, execute when runnable, and build the report."""

    prepared = prepare_analysis(options)
    outcome = execute_prepared(prepared)
    return build_report(
        source_file=str(prepared.path),
        requested_scenario=options.scenario,
        extraction=prepared.extraction,
        hypothesis=prepared.hypothesis,
        readiness=outcome.readiness,
        execution=outcome.execution,
        analysis_status=outcome.analysis_status,
        limitations=outcome.limitations,
        llm_fallback=prepared.llm_diagnostics,
        fact_provenance=prepared.llm_result.fact_provenance,
        execution_failure=outcome.failure,
    )


def _resolve_settings(options: AnalysisOptions) -> AnalysisSettings:
    retry_statuses: tuple[int, ...] | None = None
    retry_provenance: str | None = None
    server_error_status: int | None = None
    server_error_provenance: str | None = None
    if options.scenario == "retryable-error":
        if options.retry_statuses is None:
            retry_statuses = (503,)
            retry_provenance = "cli_default"
        else:
            retry_statuses = tuple(sorted(set(options.retry_statuses)))
            retry_provenance = "cli"
        if options.server_error_status is None:
            server_error_status = 503
            server_error_provenance = "cli_default"
        else:
            server_error_status = options.server_error_status
            server_error_provenance = "cli"

    if options.execution_timeout is None:
        execution_timeout = _DEFAULT_EXECUTION_TIMEOUT
        execution_timeout_provenance = "scenario_default"
    else:
        execution_timeout = float(options.execution_timeout)
        execution_timeout_provenance = "cli"

    timeout_mode: str | None = None
    timeout_mode_provenance: str | None = None
    timeout_invariant: str | None = None
    timeout_invariant_provenance: str | None = None
    if options.scenario == "client-timeout":
        if options.timeout_mode is None:
            timeout_mode = DEFAULT_TIMEOUT_MODE
            timeout_mode_provenance = "scenario_default"
        else:
            timeout_mode = options.timeout_mode
            timeout_mode_provenance = "cli"
        if options.timeout_invariant is None:
            timeout_invariant = DEFAULT_TIMEOUT_INVARIANT
            timeout_invariant_provenance = "scenario_default"
        else:
            timeout_invariant = options.timeout_invariant
            timeout_invariant_provenance = "cli"

    return AnalysisSettings(
        retry_statuses=retry_statuses,
        retry_provenance=retry_provenance,
        server_error_status=server_error_status,
        server_error_provenance=server_error_provenance,
        execution_timeout=execution_timeout,
        execution_timeout_provenance=execution_timeout_provenance,
        timeout_mode=timeout_mode,
        timeout_mode_provenance=timeout_mode_provenance,
        timeout_invariant=timeout_invariant,
        timeout_invariant_provenance=timeout_invariant_provenance,
    )


def _validate_options(options: AnalysisOptions) -> None:
    if options.scenario not in SUPPORTED_SCENARIOS:
        raise AnalysisUsageError(
            f"unknown scenario {options.scenario!r}; "
            f"choose one of {', '.join(SUPPORTED_SCENARIOS)}"
        )
    if options.server_idempotency not in SUPPORTED_SERVER_IDEMPOTENCY:
        raise AnalysisUsageError("server-idempotency must be auto, on, or off")
    if options.scenario != "retryable-error":
        if options.retry_statuses is not None:
            raise AnalysisUsageError(
                "--retry-status is only valid with --scenario retryable-error"
            )
        if options.server_error_status is not None:
            raise AnalysisUsageError(
                "--server-error-status is only valid with --scenario "
                "retryable-error"
            )
    if options.scenario != "client-timeout":
        if options.timeout_mode is not None:
            raise AnalysisUsageError(
                "--timeout-mode is only valid with --scenario client-timeout"
            )
        if options.timeout_invariant is not None:
            raise AnalysisUsageError(
                "--timeout-invariant is only valid with --scenario client-timeout"
            )
    if (
        options.timeout_mode is not None
        and options.timeout_mode not in SUPPORTED_TIMEOUT_MODES
    ):
        raise AnalysisUsageError(
            "unknown timeout mode "
            f"{options.timeout_mode!r}; choose one of "
            f"{', '.join(SUPPORTED_TIMEOUT_MODES)}"
        )
    if (
        options.timeout_invariant is not None
        and options.timeout_invariant not in TIMEOUT_INVARIANTS
    ):
        raise AnalysisUsageError(
            "unknown timeout invariant "
            f"{options.timeout_invariant!r}; choose one of "
            f"{', '.join(TIMEOUT_INVARIANTS)}"
        )
    for status in options.retry_statuses or ():
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
        ):
            raise AnalysisUsageError(
                "--retry-status values must be HTTP error statuses (400-599)"
            )
    if options.server_error_status is not None and not (
        isinstance(options.server_error_status, int)
        and not isinstance(options.server_error_status, bool)
        and 400 <= options.server_error_status <= 599
    ):
        raise AnalysisUsageError(
            "--server-error-status must be an HTTP error status (400-599)"
        )
    if options.execution_timeout is not None and (
        isinstance(options.execution_timeout, bool)
        or not isinstance(options.execution_timeout, (int, float))
        or options.execution_timeout <= 0
    ):
        raise AnalysisUsageError("--execution-timeout must be a positive number")


def _readiness_for_scenario(
    scenario: str,
    hypothesis: ClientHypothesis,
    contract: RuntimeContract,
) -> ScenarioReadiness:
    if scenario == "lost-response":
        return _merge_contract(_lost_response_readiness(hypothesis), contract)
    if scenario == "retryable-error":
        return _merge_contract(_retryable_error_readiness(hypothesis), contract)
    if scenario == "client-timeout":
        return _client_timeout_readiness(hypothesis, contract)
    raise AnalysisUsageError("no scenario readiness is available")


def _select_scenario(
    options: AnalysisOptions,
    hypothesis: ClientHypothesis,
    unsupported_extraction: bool,
    contract: RuntimeContract,
) -> tuple[str | None, ScenarioReadiness]:
    contract_blockers = () if contract.supported else contract.blockers
    if options.scenario == "auto":
        lost = _lost_response_readiness(hypothesis)
        if (
            not unsupported_extraction
            and contract.supported
            and lost.runnable
        ):
            return "lost-response", lost
        blockers = [*lost.blockers, *contract_blockers]
        if unsupported_extraction:
            blockers.insert(
                0,
                f"extraction status is {hypothesis.extraction_status.value}",
            )
        blockers.append(
            "auto selection could not choose a supported scenario; "
            "specify --scenario lost-response or --scenario retryable-error"
        )
        return None, ScenarioReadiness(
            None,
            False,
            _dedupe(tuple(blockers)),
            hypothesis.unknowns,
        )
    if options.scenario == "lost-response":
        return "lost-response", _merge_contract(
            _lost_response_readiness(hypothesis), contract
        )
    if options.scenario == "client-timeout":
        return "client-timeout", _client_timeout_readiness(hypothesis, contract)
    return "retryable-error", _merge_contract(
        _retryable_error_readiness(hypothesis), contract
    )


def _lost_response_readiness(hypothesis: ClientHypothesis) -> ScenarioReadiness:
    blockers = list(hypothesis.blockers)
    path_blocker = _endpoint_path_blocker(hypothesis)
    if path_blocker is not None:
        blockers.append(path_blocker)
    return ScenarioReadiness(
        "lost-response",
        not blockers,
        _dedupe(tuple(blockers)),
        hypothesis.unknowns,
    )


def _retryable_error_readiness(
    hypothesis: ClientHypothesis,
) -> ScenarioReadiness:
    blockers: list[str] = []
    if hypothesis.http_call_count != 1:
        blockers.append(
            "retryable-error scenario requires exactly one recognized HTTP call"
        )
    if hypothesis.method.status is not FactStatus.KNOWN:
        blockers.append("HTTP method is unknown")
    elif hypothesis.method.value != "POST":
        blockers.append("runtime payment test service only models POST")
    if hypothesis.endpoint.status is not FactStatus.KNOWN:
        blockers.append("endpoint is not a known string literal")
    else:
        path_blocker = _endpoint_path_blocker(hypothesis)
        if path_blocker is not None:
            blockers.append(path_blocker)
    if hypothesis.timeout.status is not FactStatus.KNOWN:
        blockers.append("timeout is not a concrete literal")
    elif not isinstance(hypothesis.timeout.value, (int, float)) or (
        hypothesis.timeout.value <= 0
    ):
        blockers.append("timeout is not a positive number")
    if hypothesis.retry_count.status is not FactStatus.KNOWN:
        blockers.append("retry count is not resolved")
    present = hypothesis.idempotency_key_present
    if present.status not in {FactStatus.KNOWN, FactStatus.ABSENT}:
        blockers.append("idempotency key presence is unresolved")
    elif present.status is FactStatus.KNOWN:
        reused = hypothesis.idempotency_key_reused
        if reused.status is not FactStatus.KNOWN or reused.value is not True:
            blockers.append(
                "idempotency key is not known to be reused across retry attempts"
            )
    blockers.extend(hypothesis.warnings)
    return ScenarioReadiness(
        "retryable-error",
        not blockers,
        _dedupe(tuple(blockers)),
        hypothesis.unknowns,
    )


def _client_timeout_readiness(
    hypothesis: ClientHypothesis,
    contract: RuntimeContract,
) -> ScenarioReadiness:
    """Readiness for the timeout scenario.

    A retry loop, a retry budget, and a reusable idempotency key are all
    optional here: this scenario models one attempt and asks what the server did
    after the client gave up. Requiring them would re-introduce the readiness
    mistake corrected in milestone 10.
    """

    blockers: list[str] = []
    if hypothesis.http_call_count != 1:
        blockers.append(
            "client-timeout scenario requires exactly one recognized HTTP call"
        )
    if hypothesis.method.status is not FactStatus.KNOWN:
        blockers.append("HTTP method is unknown")
    elif hypothesis.method.value != "POST":
        blockers.append("runtime payment test service only models POST")
    if hypothesis.endpoint.status is not FactStatus.KNOWN:
        blockers.append("endpoint is not a known string literal")
    else:
        path_blocker = _endpoint_path_blocker(hypothesis)
        if path_blocker is not None:
            blockers.append(path_blocker)
    if hypothesis.timeout.status is not FactStatus.KNOWN:
        blockers.append("timeout is not a concrete literal")
    elif not isinstance(hypothesis.timeout.value, (int, float)) or (
        hypothesis.timeout.value <= 0
    ):
        blockers.append("timeout is not a positive number")

    return _merge_contract(
        ScenarioReadiness(
            "client-timeout",
            not blockers,
            _dedupe(tuple(blockers)),
            hypothesis.unknowns,
        ),
        contract,
    )


def _endpoint_path_blocker(hypothesis: ClientHypothesis) -> str | None:
    if hypothesis.endpoint.status is not FactStatus.KNOWN:
        return None
    endpoint = hypothesis.endpoint.value
    if not isinstance(endpoint, str):
        return "endpoint is not a string literal"
    path = urlparse(endpoint).path or "/"
    if path != "/payments":
        return (
            "runtime payment test service only models the /payments path; "
            f"source endpoint path is {path}"
        )
    return None


def _merge_contract(
    readiness: ScenarioReadiness, contract: RuntimeContract
) -> ScenarioReadiness:
    if contract.supported:
        return readiness
    return ScenarioReadiness(
        readiness.scenario,
        False,
        _dedupe((*contract.blockers, *readiness.blockers)),
        readiness.unknowns,
    )


def _execute_lost_response(
    hypothesis: ClientHypothesis,
    options: AnalysisOptions,
    source_path: Path,
    execution_timeout: float,
    execution_timeout_provenance: str,
) -> tuple[ScenarioExecution, tuple[str, ...]]:
    try:
        server_idempotency, server_provenance = _resolve_server_idempotency(
            options.server_idempotency, hypothesis
        )
    except HypothesisNotRunnable as exc:
        raise AnalysisUsageError(str(exc)) from exc

    client_sends_key = (
        hypothesis.idempotency_key_present.status is FactStatus.KNOWN
    )
    retry_count = hypothesis.retry_count.value
    timeout = hypothesis.timeout.value
    if retry_count is None or timeout is None:
        raise AnalysisUsageError(
            "retry count or timeout is not a concrete value"
        )
    max_attempts = hypothesis.max_attempts.value

    runtime_result = run_analyzed_client(
        source=source_path,
        scenario="lost-response",
        server_supports_idempotency=server_idempotency,
        retry_statuses=(),
        server_error_status=503,
        policy_max_retries=retry_count,
        policy_max_attempts=(
            max_attempts if isinstance(max_attempts, int) else None
        ),
        execution_timeout_seconds=execution_timeout,
    )
    verified = verify_duplicate_side_effects(
        runtime_result.events,
        side_effects=runtime_result.observed_side_effects,
    )
    # Reported independently of the causal verdict: an over-budget client can
    # still have a confirmed duplicate, and staying within budget is not a
    # safety proof.
    retry_budget = check_retry_budget(runtime_result.events)
    key_stability = check_idempotency_key_stability(
        runtime_result.events,
        static_key_present=_static_bool(hypothesis.idempotency_key_present),
        static_key_reused=_static_bool(hypothesis.idempotency_key_reused),
    )
    server_deduplication = _server_deduplication_block(
        enabled=server_idempotency,
        provenance=server_provenance,
        events=runtime_result.events,
    )
    settings = {
        "entrypoint": "module",
        "server_supports_idempotency": server_idempotency,
        "client_sends_idempotency_key": client_sends_key,
        "max_retries": retry_count,
        "drop_response_on_attempts": [1],
        "request_timeout": float(timeout),
        "execution_timeout_seconds": execution_timeout,
        "operation_id_source": "harness_if_absent",
    }
    provenance = {
        "entrypoint": "scenario_default",
        "server_supports_idempotency": server_provenance,
        "client_sends_idempotency_key": "derived_from_extraction",
        "max_retries": "derived_from_extraction",
        "drop_response_on_attempts": "scenario_default",
        "request_timeout": "derived_from_extraction",
        "execution_timeout_seconds": execution_timeout_provenance,
        "operation_id_source": "scenario_default",
    }
    execution = ScenarioExecution(
        scenario="lost-response",
        runner="runtime_subprocess",
        settings=settings,
        provenance=provenance,
        report=verified,
        retry_budget=retry_budget.to_dict(),
        idempotency_key=key_stability.to_dict(),
        server_deduplication=server_deduplication,
    )
    limitations: list[str] = [
        "Analyzed client executed in a subprocess; the lost response is "
        "simulated after the in-process server commits.",
        "The harness assigns X-Operation-Id when the client does not send one.",
        (
            "The retry budget is checked against the statically extracted loop "
            "bound and reported separately from the duplicate-side-effect "
            "verdict."
        ),
        (
            "Idempotency-key values are reported only as truncated SHA-256 "
            "fingerprints; raw header values are never recorded in the report."
        ),
    ]
    if server_provenance == "inferred_from_client_facts":
        limitations.append(
            "Server deduplication was inferred from the client's key presence "
            "rather than configured independently; a not_reproduced verdict "
            "that depends on it is conditional. Pass --server-idempotency on "
            "or off for independent evidence."
        )
    return execution, tuple(limitations)


def _execute_retryable_error(
    hypothesis: ClientHypothesis,
    options: AnalysisOptions,
    source_path: Path,
    retry_statuses: tuple[int, ...],
    retry_provenance: str,
    server_error_status: int,
    server_error_provenance: str,
    execution_timeout: float,
    execution_timeout_provenance: str,
) -> tuple[ScenarioExecution, tuple[str, ...]]:
    try:
        server_idempotency, server_provenance = _resolve_server_idempotency(
            options.server_idempotency, hypothesis
        )
    except HypothesisNotRunnable as exc:
        raise AnalysisUsageError(str(exc)) from exc

    client_sends_key = (
        hypothesis.idempotency_key_present.status is FactStatus.KNOWN
    )
    retry_count = hypothesis.retry_count.value
    timeout = hypothesis.timeout.value
    if retry_count is None or timeout is None:
        raise AnalysisUsageError(
            "retry count or timeout is not a concrete value"
        )

    runtime_result = run_analyzed_client(
        source=source_path,
        scenario="retryable-error",
        server_supports_idempotency=server_idempotency,
        retry_statuses=retry_statuses,
        server_error_status=server_error_status,
        policy_max_retries=retry_count,
        execution_timeout_seconds=execution_timeout,
    )
    verified = verify_retryable_error_duplicate(
        runtime_result.events,
        side_effects=runtime_result.observed_side_effects,
    )
    settings = {
        "entrypoint": "module",
        "server_supports_idempotency": server_idempotency,
        "client_sends_idempotency_key": client_sends_key,
        "retry_statuses": list(retry_statuses),
        "server_error_status": server_error_status,
        "max_retries": retry_count,
        "request_timeout": float(timeout),
        "execution_timeout_seconds": execution_timeout,
        "operation_id_source": "harness_if_absent",
    }
    provenance = {
        "entrypoint": "scenario_default",
        "server_supports_idempotency": server_provenance,
        "client_sends_idempotency_key": "derived_from_extraction",
        "retry_statuses": retry_provenance,
        "server_error_status": server_error_provenance,
        "max_retries": "derived_from_extraction",
        "request_timeout": "derived_from_extraction",
        "execution_timeout_seconds": execution_timeout_provenance,
        "operation_id_source": "scenario_default",
    }
    key_stability = check_idempotency_key_stability(
        runtime_result.events,
        static_key_present=_static_bool(hypothesis.idempotency_key_present),
        static_key_reused=_static_bool(hypothesis.idempotency_key_reused),
    )
    server_deduplication = _server_deduplication_block(
        enabled=server_idempotency,
        provenance=server_provenance,
        events=runtime_result.events,
    )
    execution = ScenarioExecution(
        scenario="retryable-error",
        runner="runtime_subprocess",
        settings=settings,
        provenance=provenance,
        report=verified,
        idempotency_key=key_stability.to_dict(),
        server_deduplication=server_deduplication,
    )
    limitations: list[str] = [
        "Analyzed client executed in a subprocess; the retryable status policy "
        "is supplied by CLI flags/defaults.",
        "The harness records a retry decision when a request follows a "
        "configured retryable response.",
        (
            "That retry decision is harness-generated (provenance "
            "harness_configured): it shows a retry happened after a status the "
            "harness declared retryable, not that the client implemented the "
            "correct retry policy."
        ),
        "The harness assigns X-Operation-Id when the client does not send one.",
        "Static retry-trigger facts are reported but are not used to infer "
        "the HTTP status retry policy.",
        (
            "Idempotency-key values are reported only as truncated SHA-256 "
            "fingerprints; raw header values are never recorded in the report."
        ),
    ]
    if server_error_status not in retry_statuses:
        limitations.append(
            "Configured retry statuses do not include the server error status; "
            "this run tests a no-retry configuration."
        )
    if server_provenance == "inferred_from_client_facts":
        limitations.append(
            "Server deduplication was inferred from the client's key presence "
            "rather than configured independently; a not_reproduced verdict "
            "that depends on it is conditional. Pass --server-idempotency on "
            "or off for independent evidence."
        )
    return execution, tuple(limitations)


def _static_bool(fact: Any) -> bool | None:
    """Map an extracted boolean fact to a plain value, or ``None`` if unresolved."""

    status = getattr(fact, "status", None)
    if status is FactStatus.KNOWN:
        return bool(getattr(fact, "value", None))
    if status is FactStatus.ABSENT:
        return False
    return None


def _server_deduplication_block(
    *, enabled: bool, provenance: str, events: tuple
) -> dict[str, Any]:
    """Report deduplication configuration separately from client key behaviour.

    A configured server does not prove the client reuses its key, and a stable
    client key does not prove the server enforces deduplication.
    """

    replays = sum(
        1 for event in events if event.kind is EventKind.IDEMPOTENT_REPLAY
    )
    inferred = provenance == "inferred_from_client_facts"
    if inferred and enabled:
        reason = (
            "server deduplication was inferred from the client's key presence, "
            "not configured independently; conclusions that depend on it are "
            "conditional"
        )
    elif inferred:
        reason = (
            "server deduplication was inferred off because the client sends no "
            "idempotency key"
        )
    elif enabled:
        reason = "server deduplication was configured explicitly on"
    else:
        reason = "server deduplication was configured explicitly off"
    return {
        "enabled": enabled,
        "provenance": provenance,
        "inferred": inferred,
        "observed_replay_count": replays,
        "reason": reason,
    }


def _execute_client_timeout(
    hypothesis: ClientHypothesis,
    options: AnalysisOptions,
    source_path: Path,
    settings: AnalysisSettings,
) -> tuple[ScenarioExecution, tuple[str, ...]]:
    try:
        server_idempotency, server_provenance = _resolve_server_idempotency(
            options.server_idempotency, hypothesis, allow_unresolved=True
        )
    except HypothesisNotRunnable as exc:
        raise AnalysisUsageError(str(exc)) from exc

    timeout = hypothesis.timeout.value
    if timeout is None:
        raise AnalysisUsageError("timeout is not a concrete value")
    assert settings.timeout_mode is not None
    assert settings.timeout_invariant is not None
    assert settings.timeout_invariant_provenance is not None

    runtime_result = run_analyzed_client(
        source=source_path,
        scenario="client-timeout",
        server_supports_idempotency=server_idempotency,
        timeout_mode=settings.timeout_mode,
        declared_timeout_seconds=float(timeout),
        declared_timeout_source="derived_from_extraction",
        execution_timeout_seconds=settings.execution_timeout,
    )
    verified = verify_client_timeout(
        runtime_result.events,
        side_effects=runtime_result.observed_side_effects,
        invariant_id=settings.timeout_invariant,
        invariant_provenance=settings.timeout_invariant_provenance,
    )
    key_stability = check_idempotency_key_stability(
        runtime_result.events,
        static_key_present=_static_bool(hypothesis.idempotency_key_present),
        static_key_reused=_static_bool(hypothesis.idempotency_key_reused),
    )
    server_deduplication = _server_deduplication_block(
        enabled=server_idempotency,
        provenance=server_provenance,
        events=runtime_result.events,
    )
    execution_settings = {
        "entrypoint": "module",
        "timeout_mode": settings.timeout_mode,
        "timeout_invariant": settings.timeout_invariant,
        "request_timeout": float(timeout),
        "server_supports_idempotency": server_idempotency,
        "client_sends_idempotency_key": (
            hypothesis.idempotency_key_present.status is FactStatus.KNOWN
        ),
        "execution_timeout_seconds": settings.execution_timeout,
        "operation_id_source": "harness_if_absent",
    }
    provenance = {
        "entrypoint": "scenario_default",
        "timeout_mode": settings.timeout_mode_provenance,
        "timeout_invariant": settings.timeout_invariant_provenance,
        "request_timeout": "derived_from_extraction",
        "server_supports_idempotency": server_provenance,
        "client_sends_idempotency_key": "derived_from_extraction",
        "execution_timeout_seconds": settings.execution_timeout_provenance,
        "operation_id_source": "scenario_default",
    }
    execution = ScenarioExecution(
        scenario="client-timeout",
        runner="runtime_subprocess",
        settings=execution_settings,
        provenance=provenance,
        report=verified.report,
        idempotency_key=key_stability.to_dict(),
        server_deduplication=server_deduplication,
        timeout=verified.to_dict(),
    )
    limitations: list[str] = [
        (
            "The client timeout is injected by the harness in the in-process "
            "WSGI simulation; the analyzed client's own requests timeout "
            "parameter is not enforced by this harness."
        ),
        (
            "The server is suspended rather than delayed: the ordering of the "
            "timeout and any later commit is fixed by configuration, not by "
            "wall-clock timing."
        ),
        "This scenario models exactly one request attempt.",
        "The harness assigns X-Operation-Id when the client does not send one.",
    ]
    if server_provenance == "inferred_from_client_facts":
        limitations.append(
            "Server deduplication was inferred from the client's key presence "
            "rather than configured independently; this is mitigation context "
            "only and does not change the timeout verdict."
        )
    elif server_provenance == "unresolved_assumed_off":
        limitations.append(
            "The client's idempotency-key presence is unresolved, so server "
            "deduplication was assumed off for this run."
        )
    return execution, tuple(limitations)


def _resolve_server_idempotency(
    mode: str,
    hypothesis: ClientHypothesis,
    *,
    allow_unresolved: bool = False,
) -> tuple[bool, str]:
    if mode == "on":
        return True, "cli"
    if mode == "off":
        return False, "cli"
    present = hypothesis.idempotency_key_present
    if present.status is FactStatus.KNOWN:
        return True, "inferred_from_client_facts"
    if present.status is FactStatus.ABSENT:
        return False, "inferred_from_client_facts"
    if allow_unresolved:
        # The timeout scenario does not need a key to be configured. Rather than
        # blocking on an irrelevant fact, assume deduplication is off and label
        # that assumption explicitly.
        return False, "unresolved_assumed_off"
    raise HypothesisNotRunnable(("idempotency key presence is unresolved",))


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))