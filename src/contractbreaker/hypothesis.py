"""Deterministic hypotheses derived from AST extraction facts.

This module never issues a bug verdict. It summarizes client-side retry,
timeout, and idempotency facts, lists unresolved evidence, and only becomes
runnable when the facts needed to configure the existing reference scenario are
known. The causal verifier remains the sole source of ``confirmed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ast_extractor import extract_from_file, extract_from_source
from .extraction import (
    ExtractionResult,
    ExtractionStatus,
    Fact,
    FactStatus,
    HttpCallFact,
    RetryLoopFact,
)

_UNCERTAIN_STATUSES = {
    FactStatus.UNKNOWN,
    FactStatus.AMBIGUOUS,
    FactStatus.UNSUPPORTED,
}


class HypothesisNotRunnable(RuntimeError):
    """Raised when static evidence is insufficient to configure a scenario."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__("; ".join(blockers))


@dataclass(frozen=True)
class ClientHypothesis:
    """A deterministic summary of one client's extracted behavior."""

    file: str
    extraction_status: ExtractionStatus
    http_call_count: int
    method: Fact[str]
    endpoint: Fact[str]
    timeout: Fact[int | float | None]
    max_attempts: Fact[int]
    retry_count: Fact[int]
    retry_triggers: Fact[tuple[str, ...]]
    idempotency_key_present: Fact[bool]
    idempotency_key_reused: Fact[bool]
    unknowns: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    runtime_verification_required: bool = True

    @property
    def scenario_ready(self) -> bool:
        """Whether the facts are sufficient to configure the runtime scenario."""

        return not self.blockers


@dataclass(frozen=True)
class ScenarioRecommendation:
    """Client-side configuration that can be mapped to the reference scenario."""

    client_sends_idempotency_key: bool
    max_retries: int
    request_timeout: float
    notes: tuple[str, ...] = ()


def hypothesize_from_file(path: str | Path) -> ClientHypothesis:
    """Extract a file and derive a deterministic client hypothesis."""

    return hypothesize_from_extraction(extract_from_file(path))


def hypothesize_from_source(
    source: str, *, filename: str = "<string>"
) -> ClientHypothesis:
    """Extract source text and derive a deterministic client hypothesis."""

    return hypothesize_from_extraction(
        extract_from_source(source, filename=filename)
    )


def hypothesize_from_extraction(result: ExtractionResult) -> ClientHypothesis:
    """Summarize extraction facts without inventing missing values."""

    warnings = tuple(
        f"{warning.code}: {warning.message}" for warning in result.warnings
    )
    if len(result.calls) != 1:
        reason = (
            "no recognized HTTP call"
            if not result.calls
            else "multiple recognized HTTP calls; hypothesis requires exactly one"
        )
        fact = Fact.ambiguous(reason) if result.calls else Fact.absent(reason)
        unknown_fact = Fact.unknown(reason)
        unknowns = _dedupe((reason, *warnings))
        return ClientHypothesis(
            file=result.file,
            extraction_status=result.status,
            http_call_count=len(result.calls),
            method=fact,
            endpoint=fact,
            timeout=fact,
            max_attempts=fact,
            retry_count=fact,
            retry_triggers=unknown_fact,
            idempotency_key_present=unknown_fact,
            idempotency_key_reused=unknown_fact,
            unknowns=unknowns,
            blockers=unknowns,
            warnings=warnings,
        )

    call = result.calls[0]
    loop = _find_retry_loop(result, call)
    max_attempts = (
        loop.max_attempts
        if loop is not None
        else Fact.absent("call is not inside a recognized retry loop")
    )
    retry_count = (
        loop.retry_count
        if loop is not None
        else Fact.absent("call is not inside a recognized retry loop")
    )
    triggers = (
        loop.triggers
        if loop is not None
        else Fact.absent("call is not inside a recognized retry loop")
    )
    present, reused = _idempotency_facts(call)

    unknowns = _collect_unknowns(
        call,
        max_attempts,
        retry_count,
        triggers,
        present,
        reused,
        warnings,
    )
    blockers = _collect_blockers(
        result,
        call,
        loop,
        triggers,
        present,
        reused,
        warnings,
    )
    return ClientHypothesis(
        file=result.file,
        extraction_status=result.status,
        http_call_count=1,
        method=call.method,
        endpoint=call.endpoint,
        timeout=call.timeout,
        max_attempts=max_attempts,
        retry_count=retry_count,
        retry_triggers=triggers,
        idempotency_key_present=present,
        idempotency_key_reused=reused,
        unknowns=unknowns,
        blockers=blockers,
        warnings=warnings,
    )


def recommend_scenario(hypothesis: ClientHypothesis) -> ScenarioRecommendation:
    """Translate a runnable hypothesis into client-side scenario settings."""

    if not hypothesis.scenario_ready:
        raise HypothesisNotRunnable(hypothesis.blockers)

    present = hypothesis.idempotency_key_present
    if present.status is FactStatus.KNOWN:
        client_sends_key = True
    elif present.status is FactStatus.ABSENT:
        client_sends_key = False
    else:
        raise HypothesisNotRunnable(
            ("idempotency key presence is not resolved",)
        )

    retry_count = hypothesis.retry_count.value
    timeout = hypothesis.timeout.value
    if retry_count is None or timeout is None:
        raise HypothesisNotRunnable(
            ("retry count or timeout is not a concrete value",)
        )

    return ScenarioRecommendation(
        client_sends_idempotency_key=client_sends_key,
        max_retries=retry_count,
        request_timeout=float(timeout),
        notes=hypothesis.unknowns,
    )


def _find_retry_loop(
    result: ExtractionResult, call: HttpCallFact
) -> RetryLoopFact | None:
    if call.retry_loop is None:
        return None
    return next(
        (loop for loop in result.retry_loops if loop.location == call.retry_loop),
        None,
    )


def _idempotency_facts(call: HttpCallFact) -> tuple[Fact[bool], Fact[bool]]:
    key = call.idempotency_key
    if key.status is FactStatus.KNOWN:
        return (
            Fact.known(True, "Idempotency-Key header is present"),
            key.value.reused_across_attempts
            if key.value is not None
            else Fact.unknown("idempotency key details are missing"),
        )
    if key.status is FactStatus.ABSENT:
        return (
            Fact.absent("no Idempotency-Key header found"),
            Fact.absent("no idempotency key to reuse"),
        )
    return (
        Fact(key.status, reason=key.reason),
        Fact.unknown("key reuse is unknown because key presence is unresolved"),
    )


def _collect_unknowns(
    call: HttpCallFact,
    max_attempts: Fact[int],
    retry_count: Fact[int],
    triggers: Fact[tuple[str, ...]],
    present: Fact[bool],
    reused: Fact[bool],
    warnings: tuple[str, ...],
) -> tuple[str, ...]:
    entries: list[str] = []
    for label, fact in (
        ("method", call.method),
        ("endpoint", call.endpoint),
        ("timeout", call.timeout),
        ("max_attempts", max_attempts),
        ("retry_count", retry_count),
        ("retry_triggers", triggers),
        ("idempotency_key_present", present),
        ("idempotency_key_reused", reused),
    ):
        if fact.status in _UNCERTAIN_STATUSES:
            entries.append(f"{label}: {fact.reason or fact.status.value}")
    for header in call.headers:
        if header.name.status in _UNCERTAIN_STATUSES:
            entries.append(
                f"header name: {header.name.reason or header.name.status.value}"
            )
        if header.value.status in _UNCERTAIN_STATUSES:
            header_name = header.name.value or "<unknown>"
            entries.append(
                f"header {header_name}: "
                f"{header.value.reason or header.value.status.value}"
            )
    entries.extend(warnings)
    return _dedupe(tuple(entries))


def _collect_blockers(
    result: ExtractionResult,
    call: HttpCallFact,
    loop: RetryLoopFact | None,
    triggers: Fact[tuple[str, ...]],
    present: Fact[bool],
    reused: Fact[bool],
    warnings: tuple[str, ...],
) -> tuple[str, ...]:
    blockers: list[str] = []
    if result.status in {
        ExtractionStatus.PARSE_ERROR,
        ExtractionStatus.NO_HTTP_CALLS,
        ExtractionStatus.UNSUPPORTED,
    }:
        blockers.append(f"extraction status is {result.status.value}")
    if call.method.status is not FactStatus.KNOWN:
        blockers.append("HTTP method is unknown")
    elif call.method.value != "POST":
        blockers.append("reference scenario only models POST payments")
    if call.endpoint.status is not FactStatus.KNOWN:
        blockers.append("endpoint is not a known string literal")
    if call.timeout.status is not FactStatus.KNOWN:
        blockers.append("timeout is not a concrete literal")
    elif not isinstance(call.timeout.value, (int, float)) or call.timeout.value <= 0:
        blockers.append("timeout is not a positive number")
    if loop is None or loop.retry_count.status is not FactStatus.KNOWN:
        blockers.append("retry count is not resolved")
    if triggers.status is not FactStatus.KNOWN:
        blockers.append("retry-triggering exceptions are not fully identified")
    elif not any(
        "Timeout" in trigger or "ConnectionError" in trigger
        for trigger in triggers.value or ()
    ):
        blockers.append("retry triggers do not include a timeout or connection error")
    if present.status not in {FactStatus.KNOWN, FactStatus.ABSENT}:
        blockers.append("idempotency key presence is unresolved")
    elif present.status is FactStatus.KNOWN:
        if reused.status is not FactStatus.KNOWN or reused.value is not True:
            blockers.append(
                "idempotency key is not known to be reused across retry attempts"
            )
    blockers.extend(warnings)
    return _dedupe(tuple(blockers))


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))