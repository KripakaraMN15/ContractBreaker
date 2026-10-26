"""End-to-end analysis pipeline used by the CLI.

The pipeline is deliberately the only place that connects extraction,
hypothesis generation, scenario execution, and verification. The CLI only
parses arguments, renders output, writes files, and maps reports to exit codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from examples.payments.retryable_error_scenario import (
    RetryableErrorScenarioConfig,
    run_retryable_error_scenario,
)
from examples.payments.scenario import (
    PaymentScenarioConfig,
    run_payment_scenario,
)

from .ast_extractor import extract_from_source
from .extraction import ExtractionStatus, FactStatus
from .hypothesis import (
    ClientHypothesis,
    HypothesisNotRunnable,
    hypothesize_from_extraction,
    recommend_scenario,
)
from .report import ScenarioExecution, ScenarioReadiness, build_report

SUPPORTED_SCENARIOS = ("auto", "lost-response", "retryable-error")
SUPPORTED_SERVER_IDEMPOTENCY = ("auto", "on", "off")
_UNSUPPORTED_EXTRACTION = {
    ExtractionStatus.PARSE_ERROR,
    ExtractionStatus.NO_HTTP_CALLS,
    ExtractionStatus.UNSUPPORTED,
}


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


def analyze(options: AnalysisOptions) -> dict[str, Any]:
    """Run extraction, readiness checks, the scenario, and the verifier."""

    _validate_options(options)
    path = Path(options.source)
    if not path.is_file():
        raise SourceReadError(f"source file not found: {path}")
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceReadError(f"source file could not be read: {path}") from exc

    extraction = extract_from_source(source, filename=str(path))
    hypothesis = hypothesize_from_extraction(extraction)
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

    selected, readiness = _select_scenario(
        options, hypothesis, unsupported_extraction
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

    execution: ScenarioExecution | None = None
    limitations: list[str] = []
    if analysis_status == "executed":
        if selected == "lost-response":
            execution, execution_limits = _execute_lost_response(
                hypothesis, options
            )
        elif selected == "retryable-error":
            assert retry_statuses is not None
            assert retry_provenance is not None
            assert server_error_status is not None
            assert server_error_provenance is not None
            execution, execution_limits = _execute_retryable_error(
                hypothesis,
                options,
                retry_statuses,
                retry_provenance,
                server_error_status,
                server_error_provenance,
            )
        else:
            raise AnalysisUsageError("no runnable scenario was selected")
        limitations.extend(execution_limits)
    elif analysis_status == "unsupported_input":
        limitations.append(
            "No runtime scenario was executed because the source is "
            "unsupported or has no recognized requests call."
        )
    else:
        limitations.append(
            "No runtime scenario was executed because required facts for the "
            "requested scenario were unresolved."
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
    )


def _validate_options(options: AnalysisOptions) -> None:
    if options.scenario not in SUPPORTED_SCENARIOS:
        raise AnalysisUsageError(
            f"unknown scenario {options.scenario!r}; "
            f"choose one of {', '.join(SUPPORTED_SCENARIOS)}"
        )
    if options.server_idempotency not in SUPPORTED_SERVER_IDEMPOTENCY:
        raise AnalysisUsageError(
            "server-idempotency must be auto, on, or off"
        )
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
        if not isinstance(status, int) or not 400 <= status <= 599:
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


def _select_scenario(
    options: AnalysisOptions,
    hypothesis: ClientHypothesis,
    unsupported_extraction: bool,
) -> tuple[str | None, ScenarioReadiness]:
    if options.scenario == "auto":
        lost = _lost_response_readiness(hypothesis)
        if not unsupported_extraction and lost.runnable:
            return "lost-response", lost
        blockers = list(lost.blockers)
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
        return "lost-response", _lost_response_readiness(hypothesis)
    return "retryable-error", _retryable_error_readiness(hypothesis)


def _lost_response_readiness(hypothesis: ClientHypothesis) -> ScenarioReadiness:
    if hypothesis.scenario_ready:
        return ScenarioReadiness(
            "lost-response", True, (), hypothesis.unknowns
        )
    blockers = hypothesis.blockers or (
        "hypothesis is not runnable for the lost-response scenario",
    )
    return ScenarioReadiness(
        "lost-response", False, blockers, hypothesis.unknowns
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
        blockers.append("reference scenario only models POST payments")
    if hypothesis.endpoint.status is not FactStatus.KNOWN:
        blockers.append("endpoint is not a known string literal")
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


def _execute_lost_response(
    hypothesis: ClientHypothesis,
    options: AnalysisOptions,
) -> tuple[ScenarioExecution, tuple[str, ...]]:
    try:
        recommendation = recommend_scenario(hypothesis)
        server_idempotency, server_provenance = _resolve_server_idempotency(
            options.server_idempotency, hypothesis
        )
    except HypothesisNotRunnable as exc:
        raise AnalysisUsageError(str(exc)) from exc

    config = PaymentScenarioConfig(
        server_supports_idempotency=server_idempotency,
        client_sends_idempotency_key=(
            recommendation.client_sends_idempotency_key
        ),
        max_retries=recommendation.max_retries,
        drop_response_on_attempts=frozenset({1}),
        request_timeout=recommendation.request_timeout,
    )
    settings = {
        "server_supports_idempotency": server_idempotency,
        "client_sends_idempotency_key": (
            recommendation.client_sends_idempotency_key
        ),
        "max_retries": recommendation.max_retries,
        "drop_response_on_attempts": [1],
        "request_timeout": recommendation.request_timeout,
    }
    provenance = {
        "server_supports_idempotency": server_provenance,
        "client_sends_idempotency_key": "derived_from_extraction",
        "max_retries": "derived_from_extraction",
        "drop_response_on_attempts": "scenario_default",
        "request_timeout": "derived_from_extraction",
    }
    execution = ScenarioExecution(
        scenario="lost-response",
        runner="run_payment_scenario",
        settings=settings,
        provenance=provenance,
        report=run_payment_scenario(config),
    )
    limitations: list[str] = [
        "Lost response is simulated after the in-process server commits.",
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
    retry_statuses: tuple[int, ...],
    retry_provenance: str,
    server_error_status: int,
    server_error_provenance: str,
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

    config = RetryableErrorScenarioConfig(
        server_supports_idempotency=server_idempotency,
        client_sends_idempotency_key=client_sends_key,
        retry_statuses=frozenset(retry_statuses),
        max_retries=retry_count,
        request_timeout=float(timeout),
        server_error_status=server_error_status,
    )
    settings = {
        "server_supports_idempotency": server_idempotency,
        "client_sends_idempotency_key": client_sends_key,
        "retry_statuses": list(retry_statuses),
        "max_retries": retry_count,
        "request_timeout": float(timeout),
        "server_error_status": server_error_status,
    }
    provenance = {
        "server_supports_idempotency": server_provenance,
        "client_sends_idempotency_key": "derived_from_extraction",
        "retry_statuses": retry_provenance,
        "max_retries": "derived_from_extraction",
        "request_timeout": "derived_from_extraction",
        "server_error_status": server_error_provenance,
    }
    execution = ScenarioExecution(
        scenario="retryable-error",
        runner="run_retryable_error_scenario",
        settings=settings,
        provenance=provenance,
        report=run_retryable_error_scenario(config),
    )
    limitations: list[str] = [
        "Retryable status policy is supplied by CLI flags/defaults and is "
        "not statically extracted from the source.",
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