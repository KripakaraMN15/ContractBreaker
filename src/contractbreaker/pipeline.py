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
from .extraction import ExtractionStatus, FactStatus
from .hypothesis import (
    ClientHypothesis,
    HypothesisNotRunnable,
    hypothesize_from_extraction,
)
from .llm import LLMClient
from .llm_fallback import LLMFallbackResult, run_llm_fallback
from .report import ScenarioExecution, ScenarioReadiness, build_report
from .retryable_error_verifier import verify_retryable_error_duplicate
from .runtime import RuntimeExecutionError, run_analyzed_client
from .runtime_contract import RuntimeContract, check_runtime_contract
from .verifier import verify_duplicate_side_effects

SUPPORTED_SCENARIOS = ("auto", "lost-response", "retryable-error")
SUPPORTED_SERVER_IDEMPOTENCY = ("auto", "on", "off")
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


def analyze(options: AnalysisOptions) -> dict[str, Any]:
    """Extract, configure, execute the analyzed client, and verify its trace."""

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
        analysis_status = "unsupported_input"
    elif not readiness.runnable:
        analysis_status = "not_runnable"
    else:
        analysis_status = "executed"

    llm_diagnostics = llm_result.diagnostics
    if options.llm_fallback:
        before_readiness = (
            None
            if selected is None or unsupported_extraction
            else _readiness_for_scenario(
                selected, hypothesis_before, contract
            )
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

    execution: ScenarioExecution | None = None
    limitations: list[str] = []
    if analysis_status == "executed":
        try:
            if selected == "lost-response":
                execution, execution_limits = _execute_lost_response(
                    hypothesis,
                    options,
                    path,
                    execution_timeout,
                    execution_timeout_provenance,
                )
            elif selected == "retryable-error":
                assert retry_statuses is not None
                assert retry_provenance is not None
                assert server_error_status is not None
                assert server_error_provenance is not None
                execution, execution_limits = _execute_retryable_error(
                    hypothesis,
                    options,
                    path,
                    retry_statuses,
                    retry_provenance,
                    server_error_status,
                    server_error_provenance,
                    execution_timeout,
                    execution_timeout_provenance,
                )
            else:
                raise AnalysisUsageError("no runnable scenario was selected")
            limitations.extend(execution_limits)
        except RuntimeExecutionError as exc:
            failure = f"runtime execution failed: {exc}"
            readiness = ScenarioReadiness(
                selected,
                False,
                _dedupe((failure, *readiness.blockers)),
                hypothesis.unknowns,
            )
            analysis_status = "not_runnable"
            execution = None
            limitations.append(failure)

    if analysis_status == "unsupported_input":
        limitations.append(
            "No runtime scenario was executed because the source is "
            "unsupported or has no recognized requests call."
        )
    elif analysis_status == "not_runnable" and not execution:
        limitations.extend(
            (
                "No runtime scenario was executed because required facts or "
                "the runtime execution contract were unresolved.",
                "The analyzed source was not substituted with the bundled "
                "reference client.",
            )
        )
        if options.scenario == "auto":
            limitations.append(
                "Automatic selection currently supports only a runnable "
                "lost-response hypothesis; specify a scenario explicitly."
            )

    return build_report(
        source_file=str(path),
        requested_scenario=options.scenario,
        extraction=extraction,
        hypothesis=hypothesis,
        readiness=readiness,
        execution=execution,
        analysis_status=analysis_status,
        limitations=limitations,
        llm_fallback=llm_diagnostics,
        fact_provenance=llm_result.fact_provenance,
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
    for status in options.retry_statuses or ():
        if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
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

    runtime_result = run_analyzed_client(
        source=source_path,
        scenario="lost-response",
        server_supports_idempotency=server_idempotency,
        retry_statuses=(),
        server_error_status=503,
        policy_max_retries=retry_count,
        execution_timeout_seconds=execution_timeout,
    )
    verified = verify_duplicate_side_effects(
        runtime_result.events,
        side_effects=runtime_result.observed_side_effects,
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
    )
    limitations: list[str] = [
        "Analyzed client executed in a subprocess; the lost response is "
        "simulated after the in-process server commits.",
        "The harness assigns X-Operation-Id when the client does not send one.",
    ]
    if server_provenance == "derived_from_extraction":
        limitations.append(
            "Server idempotency was inferred from the client's extracted "
            "idempotency-key facts; override with --server-idempotency."
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
    execution = ScenarioExecution(
        scenario="retryable-error",
        runner="runtime_subprocess",
        settings=settings,
        provenance=provenance,
        report=verified,
    )
    limitations: list[str] = [
        "Analyzed client executed in a subprocess; the retryable status policy "
        "is supplied by CLI flags/defaults.",
        "The harness records a retry decision when a request follows a "
        "configured retryable response.",
        "The harness assigns X-Operation-Id when the client does not send one.",
        "Static retry-trigger facts are reported but are not used to infer "
        "the HTTP status retry policy.",
    ]
    if server_error_status not in retry_statuses:
        limitations.append(
            "Configured retry statuses do not include the server error status; "
            "this run tests a no-retry configuration."
        )
    if server_provenance == "derived_from_extraction":
        limitations.append(
            "Server idempotency was inferred from the client's extracted "
            "idempotency-key facts; override with --server-idempotency."
        )
    return execution, tuple(limitations)


def _resolve_server_idempotency(
    mode: str, hypothesis: ClientHypothesis
) -> tuple[bool, str]:
    if mode == "on":
        return True, "cli"
    if mode == "off":
        return False, "cli"
    present = hypothesis.idempotency_key_present
    if present.status is FactStatus.KNOWN:
        return True, "derived_from_extraction"
    if present.status is FactStatus.ABSENT:
        return False, "derived_from_extraction"
    raise HypothesisNotRunnable(("idempotency key presence is unresolved",))


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))