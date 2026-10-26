"""Conservative validation and merge layer for LLM-proposed extraction facts."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .extraction import (
    ExtractionResult,
    ExtractionStatus,
    Fact,
    FactStatus,
    IdempotencyKeyFact,
    SourceLocation,
)
from .hypothesis import ClientHypothesis, hypothesize_from_extraction
from .llm import (
    ALLOWED_FACT_TYPES,
    DEFAULT_LLM_MODEL,
    LLMClient,
    LLMInvalidOutput,
    LLMRequest,
    LLMUnavailable,
    LLMUsage,
    create_default_llm_client,
    estimate_cost_usd,
)

_UNCERTAIN = {
    FactStatus.UNKNOWN,
    FactStatus.AMBIGUOUS,
    FactStatus.UNSUPPORTED,
}
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_TRIGGER_CANONICAL = {
    "Timeout": "requests.exceptions.Timeout",
    "ReadTimeout": "requests.exceptions.ReadTimeout",
    "ConnectTimeout": "requests.exceptions.ConnectTimeout",
    "ConnectionError": "requests.exceptions.ConnectionError",
}
_MAX_REASON = 600
_MAX_EVIDENCE_LINES = 300


@dataclass(frozen=True)
class EvidenceSpan:
    start_line: int
    end_line: int
    excerpt: str


@dataclass(frozen=True)
class FactProposal:
    target_id: str
    fact_type: str
    value: Any
    status: FactStatus
    evidence: tuple[EvidenceSpan, ...]
    reason: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class LLMFallbackResult:
    """Merged extraction/hypothesis plus report diagnostics."""

    extraction: ExtractionResult
    hypothesis: ClientHypothesis
    diagnostics: dict[str, Any]
    fact_provenance: dict[str, str]


@dataclass(frozen=True)
class _Unresolved:
    key: tuple[str, str]
    target_id: str
    fact_type: str
    current_status: FactStatus
    current_value: Any
    reason: str | None
    mergeable: bool
    call_index: int | None = None
    loop_index: int | None = None
    loop_location: SourceLocation | None = None


def run_llm_fallback(
    *,
    source: str,
    extraction: ExtractionResult,
    hypothesis: ClientHypothesis,
    enabled: bool,
    client: LLMClient | None = None,
    model: str | None = None,
) -> LLMFallbackResult:
    """Optionally request validated LLM proposals and merge compatible facts."""

    provenance = _initial_provenance()
    if not enabled:
        return LLMFallbackResult(
            extraction=extraction,
            hypothesis=hypothesis,
            diagnostics=_disabled_diagnostics(),
            fact_provenance=provenance,
        )

    model_name = model or (
        client.model if client is not None else (
            os.environ.get("CONTRACTBREAKER_LLM_MODEL") or DEFAULT_LLM_MODEL
        )
    )
    unresolved = _collect_unresolved(extraction)
    if not unresolved:
        diagnostics = _base_diagnostics(
            outcome="not_needed",
            model=model_name,
            api_called=False,
            error=None,
            unresolved=(),
            usage=None,
            cost=None,
        )
        diagnostics["lost_response_ready_before"] = hypothesis.scenario_ready
        diagnostics["lost_response_ready_after"] = hypothesis.scenario_ready
        diagnostics["lost_response_ready_changed"] = False
        return LLMFallbackResult(
            extraction=extraction,
            hypothesis=hypothesis,
            diagnostics=diagnostics,
            fact_provenance=provenance,
        )

    excerpt, allowed_lines = _build_excerpt(source, extraction)
    request = LLMRequest(
        filename=Path(extraction.file).name,
        excerpt=excerpt,
        unresolved=tuple(_unresolved_dict(item) for item in unresolved),
        allowed_fact_types=ALLOWED_FACT_TYPES,
        constraints=_constraints(),
    )

    api_called = False
    try:
        if client is None:
            client = create_default_llm_client()
        api_called = True
        response = client.extract(request)
    except LLMUnavailable as exc:
        diagnostics = _base_diagnostics(
            outcome="unavailable",
            model=model_name,
            api_called=api_called,
            error=f"{exc.code}: {exc.message}",
            unresolved=tuple(_unresolved_dict(item) for item in unresolved),
            usage=None,
            cost=None,
        )
        diagnostics.update(_readiness_diagnostics(hypothesis, hypothesis))
        return LLMFallbackResult(extraction, hypothesis, diagnostics, provenance)
    except LLMInvalidOutput as exc:
        diagnostics = _base_diagnostics(
            outcome="invalid_output",
            model=model_name,
            api_called=api_called,
            error=f"{exc.code}: {exc.message}",
            unresolved=tuple(_unresolved_dict(item) for item in unresolved),
            usage=None,
            cost=None,
        )
        diagnostics.update(_readiness_diagnostics(hypothesis, hypothesis))
        return LLMFallbackResult(extraction, hypothesis, diagnostics, provenance)
    except Exception as exc:
        diagnostics = _base_diagnostics(
            outcome="unavailable",
            model=model_name,
            api_called=api_called,
            error=f"unexpected_error: {type(exc).__name__}: {str(exc)[:200]}",
            unresolved=tuple(_unresolved_dict(item) for item in unresolved),
            usage=None,
            cost=None,
        )
        diagnostics.update(_readiness_diagnostics(hypothesis, hypothesis))
        return LLMFallbackResult(extraction, hypothesis, diagnostics, provenance)

    usage = response.usage
    diagnostics = _base_diagnostics(
        outcome="success",
        model=response.model or model_name,
        api_called=True,
        error=None,
        unresolved=tuple(_unresolved_dict(item) for item in unresolved),
        usage=usage,
        cost=estimate_cost_usd(usage),
    )
    try:
        merged_extraction, merged_hypothesis, validation = _validate_and_merge(
            source=source,
            extraction=extraction,
            unresolved=unresolved,
            raw_proposals=response.proposals,
            allowed_lines=allowed_lines,
        )
    except Exception as exc:
        diagnostics["outcome"] = "invalid_output"
        diagnostics["error"] = (
            f"validation_error: {type(exc).__name__}: {str(exc)[:200]}"
        )
        diagnostics.update(_readiness_diagnostics(hypothesis, hypothesis))
        return LLMFallbackResult(extraction, hypothesis, diagnostics, provenance)

    diagnostics.update(validation)
    diagnostics["merged_extraction_status"] = (
        merged_extraction.status.value
    )
    diagnostics.update(_readiness_diagnostics(hypothesis, merged_hypothesis))
    return LLMFallbackResult(
        extraction=merged_extraction,
        hypothesis=merged_hypothesis,
        diagnostics=diagnostics,
        fact_provenance=validation.pop("fact_provenance"),
    )


def _collect_unresolved(extraction: ExtractionResult) -> list[_Unresolved]:
    unresolved: list[_Unresolved] = []
    for call_index, call in enumerate(extraction.calls):
        target = f"call:{call_index}"
        for fact_type, fact in (
            ("method", call.method),
            ("endpoint", call.endpoint),
            ("timeout", call.timeout),
        ):
            if fact.status in _UNCERTAIN:
                unresolved.append(
                    _Unresolved(
                        key=(target, fact_type),
                        target_id=target,
                        fact_type=fact_type,
                        current_status=fact.status,
                        current_value=fact.value,
                        reason=fact.reason,
                        mergeable=fact.status is FactStatus.UNKNOWN,
                        call_index=call_index,
                    )
                )
        if call.idempotency_key.status in _UNCERTAIN:
            for fact_type in (
                "idempotency_key_present",
                "idempotency_key_reused",
            ):
                unresolved.append(
                    _Unresolved(
                        key=(target, fact_type),
                        target_id=target,
                        fact_type=fact_type,
                        current_status=call.idempotency_key.status,
                        current_value=None,
                        reason=call.idempotency_key.reason,
                        mergeable=(
                            call.idempotency_key.status is FactStatus.UNKNOWN
                        ),
                        call_index=call_index,
                        loop_location=call.retry_loop,
                    )
                )
    for loop_index, loop in enumerate(extraction.retry_loops):
        target = f"loop:{loop_index}"
        for fact_type, fact in (
            ("max_attempts", loop.max_attempts),
            ("retry_count", loop.retry_count),
            ("retry_triggers", loop.triggers),
        ):
            if fact.status in _UNCERTAIN:
                unresolved.append(
                    _Unresolved(
                        key=(target, fact_type),
                        target_id=target,
                        fact_type=fact_type,
                        current_status=fact.status,
                        current_value=fact.value,
                        reason=fact.reason,
                        mergeable=fact.status is FactStatus.UNKNOWN,
                        loop_index=loop_index,
                        loop_location=loop.location,
                    )
                )
        if loop.triggers.status in _UNCERTAIN:
            unresolved.append(
                _Unresolved(
                    key=(target, "retryable_statuses"),
                    target_id=target,
                    fact_type="retryable_statuses",
                    current_status=FactStatus.UNKNOWN,
                    current_value=None,
                    reason=(
                        "the deterministic extractor does not model HTTP status "
                        "retry policies"
                    ),
                    mergeable=False,
                    loop_index=loop_index,
                    loop_location=loop.location,
                )
            )
    return unresolved


def _build_excerpt(
    source: str, extraction: ExtractionResult
) -> tuple[str, set[int]]:
    source_lines = source.splitlines()
    total_lines = len(source_lines)
    if total_lines == 0:
        return "", set()
    ranges: list[tuple[int, int]] = []
    for call in extraction.calls:
        ranges.append(
            (
                max(1, call.location.line - 20),
                min(total_lines, call.location.end_line + 20),
            )
        )
    for loop in extraction.retry_loops:
        ranges.append(
            (
                max(1, loop.location.line - 20),
                min(total_lines, loop.location.end_line + 20),
            )
        )
    for warning in extraction.warnings:
        if warning.location is not None:
            ranges.append(
                (
                    max(1, warning.location.line - 5),
                    min(total_lines, warning.location.end_line + 5),
                )
            )
    if not ranges:
        ranges = [(1, min(total_lines, _MAX_EVIDENCE_LINES))]
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    selected: set[int] = set()
    for start, end in merged:
        for line_number in range(start, end + 1):
            if len(selected) >= _MAX_EVIDENCE_LINES:
                break
            selected.add(line_number)
        if len(selected) >= _MAX_EVIDENCE_LINES:
            break
    excerpt = "\n".join(
        f"{line_number:4d}: {source_lines[line_number - 1]}"
        for line_number in sorted(selected)
    )
    return excerpt, selected


def _constraints() -> tuple[str, ...]:
    return (
        "Return proposals only for the listed target_id/fact_type pairs.",
        "Every proposal must cite one or more evidence spans from the excerpt.",
        "Evidence excerpts must exactly match the listed source lines.",
        "Use known only when the source excerpt proves the value.",
        "Use unknown, ambiguous, or unsupported with a null value when evidence is insufficient.",
        "Never infer library defaults, imported wrapper behavior, dynamic values, or missing implementations.",
        "Do not propose verdicts, scenario configuration, or remediation.",
        "For idempotency reuse, cite the header and the key assignment and explain whether it is inside or outside the retry loop.",
        "Keep reasons concise; do not provide chain-of-thought.",
    )


def _unresolved_dict(item: _Unresolved) -> dict[str, Any]:
    return {
        "target_id": item.target_id,
        "fact_type": item.fact_type,
        "current_status": item.current_status.value,
        "current_value": item.current_value,
        "reason": item.reason,
        "mergeable": item.mergeable,
    }


def _validate_and_merge(
    *,
    source: str,
    extraction: ExtractionResult,
    unresolved: list[_Unresolved],
    raw_proposals: Iterable[dict[str, Any]],
    allowed_lines: set[int],
) -> tuple[ExtractionResult, ClientHypothesis, dict[str, Any]]:
    source_lines = source.splitlines()
    unresolved_by_key = {item.key: item for item in unresolved}
    proposed: list[dict[str, Any]] = []
    parsed: list[tuple[FactProposal, _Unresolved]] = []
    rejected: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_proposals):
        errors: list[str] = []
        deterministic_conflict = _detect_deterministic_conflict(
            raw, extraction, unresolved_by_key
        )
        if deterministic_conflict is not None:
            entry = {
                "index": index,
                "target_id": deterministic_conflict["target_id"],
                "fact_type": deterministic_conflict["fact_type"],
                "value": raw.get("value") if isinstance(raw, dict) else None,
                "status": raw.get("status") if isinstance(raw, dict) else None,
                "validation": "rejected",
                "errors": [deterministic_conflict["message"]],
            }
            proposed.append(entry)
            rejected.append(entry)
            conflicts.append(deterministic_conflict)
            continue
        proposal, target, error = _parse_proposal(
            raw,
            source_lines,
            allowed_lines,
            unresolved_by_key,
            extraction,
        )
        if error is not None:
            errors.append(error)
        if proposal is None or target is None:
            entry = {
                "index": index,
                "target_id": raw.get("target_id") if isinstance(raw, dict) else None,
                "fact_type": raw.get("fact_type") if isinstance(raw, dict) else None,
                "validation": "rejected",
                "errors": errors or ["proposal is invalid"],
            }
            proposed.append(entry)
            rejected.append(entry)
            continue

        value_errors, normalized_value = _validate_value(
            proposal.fact_type, proposal.value, proposal.status
        )
        errors.extend(value_errors)
        evidence_errors = _validate_evidence(
            proposal,
            normalized_value,
            source_lines,
            allowed_lines,
            target,
        )
        errors.extend(evidence_errors)

        entry = {
            "index": index,
            "target_id": proposal.target_id,
            "fact_type": proposal.fact_type,
            "value": normalized_value,
            "status": proposal.status.value,
            "evidence": [
                {
                    "start_line": span.start_line,
                    "end_line": span.end_line,
                    "excerpt": span.excerpt,
                }
                for span in proposal.evidence
            ],
            "reason": proposal.reason,
            "limitations": list(proposal.limitations),
            "validation": "accepted_for_merge",
            "errors": errors,
        }
        proposed.append(entry)
        if errors:
            entry["validation"] = "rejected"
            rejected.append(entry)
            continue

        # Group duplicate proposals later; store normalized.
        parsed.append(
            (
                replace(proposal, value=normalized_value),
                target,
            )
        )

    grouped: dict[tuple[str, str], list[tuple[FactProposal, _Unresolved]]] = {}
    for proposal, target in parsed:
        grouped.setdefault((proposal.target_id, proposal.fact_type), []).append(
            (proposal, target)
        )

    accepted_for_merge: list[tuple[FactProposal, _Unresolved]] = []
    accepted_advisory: list[dict[str, Any]] = []
    for key, items in grouped.items():
        distinct = {
            (item[0].status, _value_key(item[0].value))
            for item in items
        }
        if len(distinct) > 1:
            conflict = {
                "kind": "contradictory_llm_proposals",
                "target_id": key[0],
                "fact_type": key[1],
                "message": "multiple proposals disagree; none applied",
            }
            conflicts.append(conflict)
            rejected.append(
                {
                    "target_id": key[0],
                    "fact_type": key[1],
                    "validation": "rejected",
                    "errors": ["contradictory proposals"],
                }
            )
            continue
        proposal, target = items[0]
        if proposal.status is not FactStatus.KNOWN:
            entry = {
                "target_id": proposal.target_id,
                "fact_type": proposal.fact_type,
                "status": proposal.status.value,
                "validation": "advisory",
                "errors": [],
            }
            accepted_advisory.append(entry)
            continue
        if not target.mergeable:
            if proposal.fact_type == "retryable_statuses":
                accepted_advisory.append(
                    {
                        "target_id": proposal.target_id,
                        "fact_type": proposal.fact_type,
                        "value": proposal.value,
                        "status": proposal.status.value,
                        "validation": "advisory",
                        "applied": False,
                        "errors": [],
                    }
                )
                continue
            conflict = {
                "kind": "non_mergeable_deterministic_status",
                "target_id": proposal.target_id,
                "fact_type": proposal.fact_type,
                "message": (
                    "deterministic status is "
                    f"{target.current_status.value}; LLM fact is advisory only"
                ),
            }
            conflicts.append(conflict)
            rejected.append(
                {
                    "target_id": proposal.target_id,
                    "fact_type": proposal.fact_type,
                    "validation": "rejected",
                    "errors": [conflict["message"]],
                }
            )
            continue
        if target.fact_type in {
            "idempotency_key_present",
            "idempotency_key_reused",
        }:
            if target.fact_type == "idempotency_key_present" and proposal.value is not True:
                rejected.append(
                    {
                        "target_id": proposal.target_id,
                        "fact_type": proposal.fact_type,
                        "validation": "rejected",
                        "errors": ["LLM-only absence is not accepted"],
                    }
                )
                continue
        accepted_for_merge.append((proposal, target))

    inconsistent_loops = _inconsistent_loop_targets(accepted_for_merge)
    if inconsistent_loops:
        accepted_for_merge = [
            (proposal, target)
            for proposal, target in accepted_for_merge
            if not (
                proposal.target_id in inconsistent_loops
                and proposal.fact_type in {"max_attempts", "retry_count"}
            )
        ]
        for target_id in inconsistent_loops:
            conflicts.append(
                {
                    "kind": "contradictory_retry_bounds",
                    "target_id": target_id,
                    "fact_type": "retry_count/max_attempts",
                    "message": "retry_count and max_attempts proposals disagree",
                }
            )
            rejected.append(
                {
                    "target_id": target_id,
                    "fact_type": "retry_count/max_attempts",
                    "validation": "rejected",
                    "errors": ["contradictory retry bounds"],
                }
            )

    if _has_idempotency_reuse_without_presence(accepted_for_merge):
        accepted_for_merge = [
            (proposal, target)
            for proposal, target in accepted_for_merge
            if target.fact_type != "idempotency_key_reused"
        ]
        rejected.append(
            {
                "target_id": "call",
                "fact_type": "idempotency_key_reused",
                "validation": "rejected",
                "errors": [
                    "key reuse requires a validated idempotency_key_present proposal"
                ],
            }
        )

    merged_extraction, provenance = _apply_facts(extraction, accepted_for_merge)
    merged_hypothesis = hypothesize_from_extraction(merged_extraction)
    accepted = []
    for proposal, target in accepted_for_merge:
        accepted.append(
            {
                "target_id": proposal.target_id,
                "fact_type": proposal.fact_type,
                "value": proposal.value,
                "status": proposal.status.value,
                "provenance": "llm_assisted",
                "applied": True,
            }
        )
    accepted.extend(accepted_advisory)

    return (
        merged_extraction,
        merged_hypothesis,
        {
            "proposed_facts": proposed,
            "accepted_facts": accepted,
            "rejected_facts": rejected,
            "conflicts": conflicts,
            "fact_provenance": provenance,
        },
    )


def _parse_proposal(
    raw: dict[str, Any],
    source_lines: list[str],
    allowed_lines: set[int],
    unresolved_by_key: dict[tuple[str, str], _Unresolved],
    extraction: ExtractionResult,
) -> tuple[FactProposal | None, _Unresolved | None, str | None]:
    if not isinstance(raw, dict):
        return None, None, "proposal must be an object"
    required = {
        "target_id",
        "fact_type",
        "value",
        "status",
        "evidence",
        "reason",
        "limitations",
    }
    if set(raw) != required:
        return None, None, "proposal has missing or extra fields"
    target_id = raw.get("target_id")
    fact_type = raw.get("fact_type")
    if not isinstance(target_id, str) or not target_id:
        return None, None, "target_id must be a non-empty string"
    if fact_type not in ALLOWED_FACT_TYPES:
        return None, None, "unsupported fact_type"
    key = (target_id, fact_type)
    target = unresolved_by_key.get(key)
    if target is None:
        # A proposal for an established fact is either a no-op or a conflict.
        current = _deterministic_fact(extraction, target_id, fact_type)
        if current is not None:
            return (
                None,
                None,
                "fact is not unresolved; deterministic fact takes precedence",
            )
        return None, None, "target/fact pair is not unresolved"
    status_raw = raw.get("status")
    try:
        status = FactStatus(status_raw)
    except (TypeError, ValueError):
        return None, None, "invalid status"
    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        return None, None, "evidence must be a non-empty list"
    evidence: list[EvidenceSpan] = []
    for item in evidence_raw:
        if not isinstance(item, dict) or set(item) != {
            "start_line",
            "end_line",
            "excerpt",
        }:
            return None, None, "evidence span has invalid fields"
        start = item.get("start_line")
        end = item.get("end_line")
        excerpt = item.get("excerpt")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(excerpt, str)
            or not 1 <= start <= end <= len(source_lines)
        ):
            return None, None, "invalid evidence line range"
        if any(line not in allowed_lines for line in range(start, end + 1)):
            return None, None, "evidence cites lines outside the submitted excerpt"
        expected = "\n".join(source_lines[start - 1 : end]).strip()
        if excerpt.strip() != expected:
            return None, None, "evidence excerpt does not match the source"
        evidence.append(EvidenceSpan(start, end, excerpt.strip()))
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, None, "reason must be non-empty"
    if len(reason) > _MAX_REASON:
        return None, None, "reason is too long"
    limitations = raw.get("limitations")
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) for item in limitations
    ):
        return None, None, "limitations must be a string list"
    return (
        FactProposal(
            target_id=target_id,
            fact_type=fact_type,
            value=raw.get("value"),
            status=status,
            evidence=tuple(evidence),
            reason=reason.strip(),
            limitations=tuple(limitations),
        ),
        target,
        None,
    )


def _validate_value(
    fact_type: str, value: Any, status: FactStatus
) -> tuple[list[str], Any]:
    if status is not FactStatus.KNOWN:
        if value is not None:
            return ["non-known statuses must use a null value"], None
        return [], None
    if fact_type == "method":
        if not isinstance(value, str) or value.upper() not in _ALLOWED_METHODS:
            return ["method must be a supported HTTP method"], value
        return [], value.upper()
    if fact_type == "endpoint":
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            return ["endpoint must be a non-empty single-line string"], value
        return [], value
    if fact_type == "timeout":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            return ["timeout must be a positive number"], value
        return [], value
    if fact_type == "retry_count":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            return ["retry_count must be a non-negative integer"], value
        return [], value
    if fact_type == "max_attempts":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
        ):
            return ["max_attempts must be a positive integer"], value
        return [], value
    if fact_type == "retry_triggers":
        if not isinstance(value, list) or not value:
            return ["retry_triggers must be a non-empty list"], value
        canonical: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return ["retry trigger must be a string"], value
            short = item.rsplit(".", 1)[-1]
            mapped = _TRIGGER_CANONICAL.get(short)
            if mapped is None:
                return [f"unsupported retry trigger: {item}"], value
            canonical.append(mapped)
        return [], tuple(dict.fromkeys(canonical))
    if fact_type in {"idempotency_key_present", "idempotency_key_reused"}:
        if not isinstance(value, bool):
            return [f"{fact_type} must be a boolean"], value
        return [], value
    if fact_type == "retryable_statuses":
        if not isinstance(value, list) or not value:
            return ["retryable_statuses must be a non-empty list"], value
        statuses: list[int] = []
        for item in value:
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 400 <= item <= 599
            ):
                return ["retryable statuses must be HTTP error codes"], value
            statuses.append(item)
        return [], tuple(sorted(set(statuses)))
    return ["unsupported fact type"], value


def _validate_evidence(
    proposal: FactProposal,
    value: Any,
    source_lines: list[str],
    allowed_lines: set[int],
    target: _Unresolved,
) -> list[str]:
    errors: list[str] = []
    combined = "\n".join(span.excerpt for span in proposal.evidence)
    lower = combined.lower()
    if proposal.status is not FactStatus.KNOWN:
        return errors
    assert value is not None
    if proposal.fact_type == "method":
        if str(value).lower() not in lower:
            errors.append("method value is not present in evidence")
    elif proposal.fact_type == "endpoint":
        if str(value) not in combined:
            errors.append("endpoint value is not present in evidence")
    elif proposal.fact_type == "timeout":
        if "timeout" not in lower:
            errors.append("timeout evidence is missing a timeout expression")
        if not _number_present(value, combined):
            errors.append("timeout value is not present in evidence")
    elif proposal.fact_type in {"retry_count", "max_attempts"}:
        if not any(token in lower for token in ("for ", "while ", "range(")):
            errors.append("retry limit evidence is missing a loop")
        if not _retry_limit_evidence_ok(
            proposal.fact_type, int(value), combined
        ):
            errors.append("retry limit value is not supported by the loop evidence")
    elif proposal.fact_type == "retry_triggers":
        for trigger in value:
            short = str(trigger).rsplit(".", 1)[-1]
            if short.lower() not in lower:
                errors.append(f"retry trigger {short} is not present in evidence")
    elif proposal.fact_type == "idempotency_key_present":
        if "idempotency-key" not in lower:
            errors.append("Idempotency-Key header is not present in evidence")
    elif proposal.fact_type == "idempotency_key_reused":
        if "idempotency-key" not in lower:
            errors.append("Idempotency-Key header is not present in evidence")
        errors.extend(_reuse_evidence_errors(proposal, target, value))
    elif proposal.fact_type == "retryable_statuses":
        for status in value:
            if not _number_present(status, combined):
                errors.append(
                    f"retryable status {status} is not present in evidence"
                )
    return errors


def _reuse_evidence_errors(
    proposal: FactProposal, target: _Unresolved, value: Any
) -> list[str]:
    if not target.loop_location:
        return ["key reuse requires a recognized retry loop"]
    pattern = re.compile(
        r"(uuid4|uuid1|token_hex|token_urlsafe|urandom|\w*key\w*\s*=|Idempotency-Key\]\s*=)",
        re.IGNORECASE,
    )
    assignment_lines: list[int] = []
    for span in proposal.evidence:
        if pattern.search(span.excerpt):
            assignment_lines.extend(range(span.start_line, span.end_line + 1))
    if not assignment_lines:
        return ["key assignment evidence is missing"]
    loop_start = target.loop_location.line
    loop_end = target.loop_location.end_line
    if value is True:
        if not any(not loop_start <= line <= loop_end for line in assignment_lines):
            return ["reused key assignment is not shown outside the retry loop"]
    else:
        if not any(loop_start <= line <= loop_end for line in assignment_lines):
            return ["fresh key assignment is not shown inside the retry loop"]
    return []


def _apply_facts(
    extraction: ExtractionResult,
    accepted: list[tuple[FactProposal, _Unresolved]],
) -> tuple[ExtractionResult, dict[str, str]]:
    calls = list(extraction.calls)
    loops = list(extraction.retry_loops)
    provenance = _initial_provenance()
    by_target: dict[str, dict[str, FactProposal]] = {}
    for proposal, target in accepted:
        by_target.setdefault(proposal.target_id, {})[proposal.fact_type] = proposal

    for target_id, facts in by_target.items():
        kind, raw_index = target_id.split(":")
        index = int(raw_index)
        if kind == "call":
            call = calls[index]
            if "method" in facts:
                call = replace(
                    call,
                    method=Fact.known(
                        facts["method"].value,
                        "LLM-assisted from validated evidence",
                    ),
                )
                provenance["method"] = "llm_assisted"
            if "endpoint" in facts:
                call = replace(
                    call,
                    endpoint=Fact.known(
                        facts["endpoint"].value,
                        "LLM-assisted from validated evidence",
                    ),
                )
                provenance["endpoint"] = "llm_assisted"
            if "timeout" in facts:
                call = replace(
                    call,
                    timeout=Fact.known(
                        facts["timeout"].value,
                        "LLM-assisted from validated evidence",
                    ),
                )
                provenance["timeout"] = "llm_assisted"
            presence = facts.get("idempotency_key_present")
            reuse = facts.get("idempotency_key_reused")
            if presence is not None:
                reuse_fact = (
                    Fact.known(
                        reuse.value,
                        "LLM-assisted from validated evidence",
                    )
                    if reuse is not None
                    else Fact.unknown("key reuse was not established")
                )
                generated_fact = (
                    Fact.known(
                        not reuse.value,
                        "derived from validated key reuse evidence",
                    )
                    if reuse is not None
                    else Fact.unknown("key generation location was not established")
                )
                span = presence.evidence[0]
                call = replace(
                    call,
                    idempotency_key=Fact.known(
                        IdempotencyKeyFact(
                            header_name="Idempotency-Key",
                            key_expression=None,
                            generated_inside_retry=generated_fact,
                            reused_across_attempts=reuse_fact,
                            location=SourceLocation(
                                file=extraction.file,
                                line=span.start_line,
                                column=1,
                                end_line=span.end_line,
                                end_column=len(
                                    span.excerpt.splitlines()[-1]
                                )
                                + 1,
                            ),
                        ),
                        "LLM-assisted from validated evidence",
                    ),
                )
                provenance["idempotency_key_present"] = "llm_assisted"
                if reuse is not None:
                    provenance["idempotency_key_reused"] = "llm_assisted"
            calls[index] = call
        else:
            loop = loops[index]
            retry = facts.get("retry_count")
            maximum = facts.get("max_attempts")
            if retry is not None and maximum is not None:
                if maximum.value != retry.value + 1:
                    continue
            if retry is not None:
                loop = replace(
                    loop,
                    retry_count=Fact.known(
                        retry.value,
                        "LLM-assisted from validated evidence",
                    ),
                    max_attempts=Fact.known(
                        retry.value + 1,
                        "derived from LLM-assisted retry_count",
                    ),
                )
                provenance["retry_count"] = "llm_assisted"
                provenance["max_attempts"] = "llm_assisted"
            elif maximum is not None:
                loop = replace(
                    loop,
                    max_attempts=Fact.known(
                        maximum.value,
                        "LLM-assisted from validated evidence",
                    ),
                    retry_count=Fact.known(
                        max(0, maximum.value - 1),
                        "derived from LLM-assisted max_attempts",
                    ),
                )
                provenance["retry_count"] = "llm_assisted"
                provenance["max_attempts"] = "llm_assisted"
            if "retry_triggers" in facts:
                loop = replace(
                    loop,
                    triggers=Fact.known(
                        facts["retry_triggers"].value,
                        "LLM-assisted from validated evidence",
                    ),
                )
                provenance["retry_triggers"] = "llm_assisted"
            loops[index] = loop

    status = extraction.status
    if status not in {
        ExtractionStatus.PARSE_ERROR,
        ExtractionStatus.NO_HTTP_CALLS,
        ExtractionStatus.UNSUPPORTED,
    }:
        status = _merged_status(calls, loops, extraction)
    return (
        replace(
            extraction,
            status=status,
            calls=tuple(calls),
            retry_loops=tuple(loops),
        ),
        provenance,
    )


def _merged_status(calls, loops, extraction: ExtractionResult) -> ExtractionStatus:
    if extraction.warnings:
        return ExtractionStatus.PARTIAL
    for call in calls:
        if call.method.status not in {FactStatus.KNOWN, FactStatus.ABSENT}:
            return ExtractionStatus.PARTIAL
        if call.endpoint.status not in {FactStatus.KNOWN, FactStatus.ABSENT}:
            return ExtractionStatus.PARTIAL
        if call.timeout.status not in {FactStatus.KNOWN, FactStatus.ABSENT}:
            return ExtractionStatus.PARTIAL
        if call.idempotency_key.status not in {
            FactStatus.KNOWN,
            FactStatus.ABSENT,
        }:
            return ExtractionStatus.PARTIAL
        if call.idempotency_key.status is FactStatus.KNOWN and call.idempotency_key.value:
            if any(
                fact.status
                not in {FactStatus.KNOWN, FactStatus.ABSENT}
                for fact in (
                    call.idempotency_key.value.generated_inside_retry,
                    call.idempotency_key.value.reused_across_attempts,
                )
            ):
                return ExtractionStatus.PARTIAL
    for loop in loops:
        if any(
            fact.status not in {FactStatus.KNOWN, FactStatus.ABSENT}
            for fact in (loop.max_attempts, loop.retry_count, loop.triggers)
        ):
            return ExtractionStatus.PARTIAL
    return ExtractionStatus.SUPPORTED


def _deterministic_fact(
    extraction: ExtractionResult, target_id: str, fact_type: str
) -> Fact | None:
    try:
        kind, raw_index = target_id.split(":")
        index = int(raw_index)
    except (ValueError, AttributeError):
        return None
    if kind == "call" and 0 <= index < len(extraction.calls):
        call = extraction.calls[index]
        return {
            "method": call.method,
            "endpoint": call.endpoint,
            "timeout": call.timeout,
            "idempotency_key_present": call.idempotency_key,
            "idempotency_key_reused": call.idempotency_key,
        }.get(fact_type)
    if kind == "loop" and 0 <= index < len(extraction.retry_loops):
        loop = extraction.retry_loops[index]
        return {
            "max_attempts": loop.max_attempts,
            "retry_count": loop.retry_count,
            "retry_triggers": loop.triggers,
        }.get(fact_type)
    return None


def _has_idempotency_reuse_without_presence(
    accepted: list[tuple[FactProposal, _Unresolved]]
) -> bool:
    by_target: dict[str, set[str]] = {}
    for proposal, _ in accepted:
        if proposal.target_id.startswith("call:"):
            by_target.setdefault(proposal.target_id, set()).add(
                proposal.fact_type
            )
    return any(
        "idempotency_key_reused" in facts
        and "idempotency_key_present" not in facts
        for facts in by_target.values()
    )


def _inconsistent_loop_targets(
    accepted: list[tuple[FactProposal, _Unresolved]],
) -> set[str]:
    by_loop: dict[str, dict[str, FactProposal]] = {}
    for proposal, _ in accepted:
        if proposal.target_id.startswith("loop:") and proposal.fact_type in {
            "retry_count",
            "max_attempts",
        }:
            by_loop.setdefault(proposal.target_id, {})[proposal.fact_type] = proposal
    inconsistent: set[str] = set()
    for target_id, facts in by_loop.items():
        if "retry_count" in facts and "max_attempts" in facts:
            if facts["max_attempts"].value != facts["retry_count"].value + 1:
                inconsistent.add(target_id)
    return inconsistent


def _detect_deterministic_conflict(
    raw: object,
    extraction: ExtractionResult,
    unresolved_by_key: dict[tuple[str, str], _Unresolved],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    target_id = raw.get("target_id")
    fact_type = raw.get("fact_type")
    if not isinstance(target_id, str) or not isinstance(fact_type, str):
        return None
    if (target_id, fact_type) in unresolved_by_key:
        return None
    if fact_type not in ALLOWED_FACT_TYPES:
        return None
    current = _deterministic_fact(extraction, target_id, fact_type)
    if current is None or raw.get("status") != "known":
        return None
    errors, normalized = _validate_value(
        fact_type, raw.get("value"), FactStatus.KNOWN
    )
    if errors:
        return None

    deterministic: Any = current.value
    if fact_type == "idempotency_key_present":
        if current.status is FactStatus.KNOWN:
            deterministic = current.value is not None
        elif current.status is FactStatus.ABSENT:
            deterministic = False
        else:
            return None
    elif fact_type == "idempotency_key_reused":
        if current.status is FactStatus.KNOWN and current.value is not None:
            reuse = current.value.reused_across_attempts
            if reuse.status is FactStatus.KNOWN:
                deterministic = reuse.value
            else:
                return None
        else:
            return None

    disagrees = (
        deterministic != normalized
        if current.status in {FactStatus.KNOWN, FactStatus.ABSENT}
        else False
    )
    if not disagrees:
        return None
    return {
        "kind": "deterministic_conflict",
        "target_id": target_id,
        "fact_type": fact_type,
        "deterministic": deterministic,
        "proposed": normalized,
        "message": (
            "LLM proposal disagrees with a deterministic fact; "
            "the deterministic value is preserved"
        ),
    }


def _initial_provenance() -> dict[str, str]:
    return {
        "method": "derived_from_extraction",
        "endpoint": "derived_from_extraction",
        "timeout": "derived_from_extraction",
        "max_attempts": "derived_from_extraction",
        "retry_count": "derived_from_extraction",
        "retry_triggers": "derived_from_extraction",
        "idempotency_key_present": "derived_from_extraction",
        "idempotency_key_reused": "derived_from_extraction",
    }


def _readiness_diagnostics(
    before: ClientHypothesis, after: ClientHypothesis
) -> dict[str, Any]:
    return {
        "lost_response_ready_before": before.scenario_ready,
        "lost_response_ready_after": after.scenario_ready,
        "lost_response_ready_changed": (
            before.scenario_ready != after.scenario_ready
        ),
    }


def _base_diagnostics(
    *,
    outcome: str,
    model: str | None,
    api_called: bool,
    error: str | None,
    unresolved: tuple[dict[str, Any], ...],
    usage: LLMUsage | None,
    cost: float | None,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "outcome": outcome,
        "model": model,
        "api_called": api_called,
        "error": error,
        "unresolved_facts": list(unresolved),
        "proposed_facts": [],
        "accepted_facts": [],
        "rejected_facts": [],
        "conflicts": [],
        "usage": usage.to_dict() if usage is not None else None,
        "estimated_cost_usd": cost,
        "runnability_before": None,
        "runnability_after": None,
        "runnability_changed": None,
        "limitations": [
            "The LLM proposes facts only; deterministic validation and the runtime verifier remain authoritative.",
            "The source excerpt is sent to the configured OpenAI model when fallback is enabled.",
        ],
    }


def _disabled_diagnostics() -> dict[str, Any]:
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


def _retry_limit_evidence_ok(
    fact_type: str, value: int, text: str
) -> bool:
    assignment_values = {
        int(match.group(1))
        for match in re.finditer(r"\b[A-Za-z_]\w*\s*=\s*(\d+)\b", text)
    }
    direct_range_values: set[int] = set()
    for match in re.finditer(r"range\s*\(([^)]*)\)", text):
        parts = [part.strip() for part in match.group(1).split(",")]
        if not 1 <= len(parts) <= 3:
            continue
        if all(re.fullmatch(r"\d+", part) for part in parts):
            numbers = [int(part) for part in parts]
            if len(numbers) == 1:
                direct_range_values.add(numbers[0])
            else:
                direct_range_values.add(max(0, numbers[1] - numbers[0]))
    if fact_type == "max_attempts":
        return value in assignment_values or value in direct_range_values
    return value in assignment_values or value + 1 in direct_range_values


def _number_present(value: Any, text: str) -> bool:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, float)):
        token = str(value)
        return re.search(
            rf"(?<![\w.]){re.escape(token)}(?:\.0+)?(?![\w.])",
            text,
        ) is not None
    return False


def _value_key(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    return value