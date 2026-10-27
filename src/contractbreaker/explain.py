"""Evidence-based explanations for analysis and workflow reports.

Deterministic explanations are always available. The optional LLM summary is
constrained to supplied report facts and is rejected when it contradicts the
runtime verdict or the observed side-effect count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .llm import (
    JSONCompletion,
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    LLMUsage,
    estimate_cost_usd,
)

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "observed",
        "expected",
        "duplicate_side_effects",
        "verdict",
    ],
    "properties": {
        "summary": {"type": "string"},
        "observed": {"type": "string"},
        "expected": {"type": "string"},
        "duplicate_side_effects": {"type": "boolean"},
        "verdict": {
            "type": "string",
            "enum": ["confirmed", "not_reproduced", "inconclusive"],
        },
    },
}

_SUMMARY_SYSTEM = (
    "You summarize a supplied ContractBreaker report. Use only facts present in "
    "the payload. Do not change the verdict, invent evidence, invent or alter "
    "side-effect counts, or speculate about code not in the report. Keep each "
    "field concise. Do not provide chain-of-thought."
)

_SIDE_EFFECT_COUNT = re.compile(r"(\d+)\s+side[\s-]?effects?", re.IGNORECASE)


@dataclass(frozen=True)
class SummaryResult:
    """An explanation string plus how it was produced."""

    text: str
    outcome: str
    model: str | None = None
    usage: LLMUsage | None = None
    estimated_cost_usd: float | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "model": self.model,
            "used_llm": self.outcome == "success",
            "usage": self.usage.to_dict() if self.usage else None,
            "estimated_cost_usd": self.estimated_cost_usd,
            "rejection_reason": self.rejection_reason,
        }


def build_deterministic_explanation(report: dict[str, Any]) -> str:
    """Explain the report using only its structured fields."""

    hypothesis = report["hypothesis"]
    execution = report["execution"]
    readiness = hypothesis["scenario_readiness"]
    provenance = hypothesis.get("fact_provenance", {})
    llm = hypothesis.get("llm_fallback", {})

    lines: list[str] = []
    lines.append(f"Scenario: {report['selected_scenario'] or 'none selected'}.")
    lines.append(
        "Static analysis: "
        f"method={_fact(hypothesis.get('method'))}, "
        f"timeout={_fact(hypothesis.get('timeout'))}, "
        f"retry_count={_fact(hypothesis.get('retry_count'))}, "
        f"retry_triggers={_fact(hypothesis.get('retry_triggers'))}, "
        f"idempotency_key={_fact(hypothesis.get('idempotency_key_present'))}, "
        f"key_reuse={_fact(hypothesis.get('idempotency_key_reused'))}."
    )
    assisted = sorted(
        name for name, origin in provenance.items() if origin == "llm_assisted"
    )
    if assisted:
        lines.append(
            "LLM-assisted facts (evidence-validated): "
            + ", ".join(assisted)
            + "."
        )
    if llm.get("enabled"):
        lines.append(
            "Fallback outcome: "
            f"{llm.get('outcome')}"
            + (f" ({llm['error']})" if llm.get("error") else "")
            + "."
        )

    if not execution["occurred"]:
        lines.append(
            "Observed: no runtime scenario was executed, so there is no "
            "runtime evidence."
        )
        lines.append(
            "Expected: a runnable scenario would execute the analyzed client "
            "against the local test service."
        )
        if readiness["blockers"]:
            lines.append("Blockers: " + "; ".join(readiness["blockers"]))
        lines.append(
            "Verdict: INCONCLUSIVE. Static analysis alone cannot confirm or "
            "clear a duplicate side effect."
        )
        return "\n".join(lines)

    events = execution["events"]
    attempts = [e for e in events if e["kind"] == "request_sent"]
    commits = [e for e in events if e["kind"] == "side_effect_committed"]
    drops = [e for e in events if e["kind"] == "response_dropped"]
    retryable = [
        e for e in events if e["kind"] == "retryable_error_returned"
    ]
    decisions = [e for e in events if e["kind"] == "retry_scheduled"]
    replays = [e for e in events if e["kind"] == "idempotent_replay"]
    side_effects = execution["observed_side_effects"]

    lines.append(
        "Observed: "
        f"{len(attempts)} request attempt(s), "
        f"{len(commits)} side-effect commit(s), "
        f"{len(drops)} dropped response(s), "
        f"{len(retryable)} retryable error response(s), "
        f"{len(decisions)} recorded retry decision(s), "
        f"{len(replays)} idempotent replay(s); "
        f"observed side effects={side_effects}."
    )
    lines.append(
        "Expected: one logical operation should produce at most one side "
        "effect for a safe client."
    )
    harness_decisions = [
        event
        for event in decisions
        if (event.get("data") or {}).get("decision_provenance")
        == "harness_configured"
    ]
    if harness_decisions:
        lines.append(
            f"{len(harness_decisions)} retry decision(s) were recorded by the "
            "harness from its configured status policy; they are not evidence "
            "that the client implemented that policy."
        )
    budget = execution.get("retry_budget")
    if isinstance(budget, dict):
        lines.append(
            "Retry budget: "
            f"{budget.get('outcome')} (observed "
            f"{budget.get('observed_attempts')} of "
            f"{budget.get('expected_max_attempts')} permitted attempt(s)); "
            f"{budget.get('reason')}"
        )
    keys = execution.get("idempotency_key")
    if isinstance(keys, dict):
        lines.append(
            "Idempotency key: "
            f"{keys.get('outcome')} "
            f"({keys.get('distinct_key_count')} distinct value(s) across "
            f"{len(keys.get('attempts') or [])} attempt(s)); "
            f"{keys.get('reason')}"
        )
    dedup = execution.get("server_deduplication")
    if isinstance(dedup, dict):
        lines.append(
            "Server deduplication: "
            f"{'on' if dedup.get('enabled') else 'off'} "
            f"[{dedup.get('provenance')}"
            f"{', inferred' if dedup.get('inferred') else ''}], "
            f"observed replays: {dedup.get('observed_replay_count')}."
        )
    duplicate = isinstance(side_effects, int) and side_effects > 1
    lines.append(
        "Duplicate side effects observed: "
        + ("yes" if duplicate else "no")
        + "."
    )
    lines.append(
        f"Verdict: {str(report['verdict']).upper()} — {report['verdict_reason']}"
    )
    lines.append(
        "Only the runtime causal verifier assigns the verdict; static "
        "extraction and the hypothesis are not proof."
    )
    return "\n".join(lines)


def summarize_report(
    report: dict[str, Any],
    *,
    enabled: bool,
    client: LLMClient | None = None,
) -> SummaryResult:
    """Optionally produce a validated LLM summary, else a deterministic one."""

    deterministic = build_deterministic_explanation(report)
    if not enabled:
        return SummaryResult(
            text=deterministic,
            outcome="disabled",
        )
    try:
        if client is None:
            from .llm import create_default_llm_client

            client = create_default_llm_client()
        completion = client.complete_json(
            system=_SUMMARY_SYSTEM,
            payload=_summary_payload(report),
            schema=SUMMARY_SCHEMA,
            schema_name="contractbreaker_summary",
        )
    except (LLMUnavailable, LLMInvalidOutput) as exc:
        return SummaryResult(
            text=deterministic,
            outcome=(
                "unavailable"
                if isinstance(exc, LLMUnavailable)
                else "invalid_output"
            ),
            rejection_reason=f"{exc.code}: {exc.message}",
        )
    except Exception as exc:
        return SummaryResult(
            text=deterministic,
            outcome="unavailable",
            rejection_reason=f"{type(exc).__name__}: {str(exc)[:200]}",
        )

    error = validate_summary(report, completion)
    if error is not None:
        return SummaryResult(
            text=deterministic,
            outcome="invalid_output",
            model=completion.model,
            usage=completion.usage,
            estimated_cost_usd=estimate_cost_usd(completion.usage),
            rejection_reason=error,
        )

    data = completion.data
    text = "\n".join(
        (
            data["summary"].strip(),
            f"Observed: {data['observed'].strip()}",
            f"Expected: {data['expected'].strip()}",
            (
                "Duplicate side effects observed: "
                + ("yes" if data["duplicate_side_effects"] else "no")
                + "."
            ),
            f"Verdict: {str(report['verdict']).upper()} — {report['verdict_reason']}",
        )
    )
    return SummaryResult(
        text=text,
        outcome="success",
        model=completion.model,
        usage=completion.usage,
        estimated_cost_usd=estimate_cost_usd(completion.usage),
    )


def validate_summary(
    report: dict[str, Any], completion: JSONCompletion
) -> str | None:
    """Return a rejection reason when the summary contradicts the report."""

    data = completion.data
    required = {
        "summary",
        "observed",
        "expected",
        "duplicate_side_effects",
        "verdict",
    }
    if set(data) != required:
        return "summary has invalid fields"
    for field in ("summary", "observed", "expected"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"summary field {field} must be a non-empty string"
    if data.get("verdict") != report["verdict"]:
        return "summary verdict does not match the runtime verdict"
    duplicate = data.get("duplicate_side_effects")
    if not isinstance(duplicate, bool):
        return "duplicate_side_effects must be a boolean"

    execution = report["execution"]
    observed = execution.get("observed_side_effects")
    executed = bool(execution.get("occurred"))
    if not executed:
        if duplicate:
            return "summary claims duplicate side effects without runtime evidence"
        return None

    expected_duplicate = isinstance(observed, int) and observed > 1
    if duplicate != expected_duplicate:
        return "summary duplicate flag contradicts the observed side effects"
    if not isinstance(observed, int):
        return "report has no observed side-effect count to validate against"

    text = " ".join(
        str(data[field]) for field in ("summary", "observed", "expected")
    )
    for match in _SIDE_EFFECT_COUNT.finditer(text):
        if int(match.group(1)) != observed:
            return (
                "summary mentions a side-effect count that contradicts the "
                "observed count"
            )
    return None


def _summary_payload(report: dict[str, Any]) -> dict[str, Any]:
    execution = report["execution"]
    hypothesis = report["hypothesis"]
    return {
        "scenario": report["selected_scenario"],
        "analysis_status": report["analysis_status"],
        "verdict": report["verdict"],
        "verdict_reason": report["verdict_reason"],
        "scenario_readiness": hypothesis["scenario_readiness"],
        "static_facts": {
            "method": hypothesis.get("method"),
            "endpoint": hypothesis.get("endpoint"),
            "timeout": hypothesis.get("timeout"),
            "retry_count": hypothesis.get("retry_count"),
            "retry_triggers": hypothesis.get("retry_triggers"),
            "idempotency_key_present": hypothesis.get(
                "idempotency_key_present"
            ),
            "idempotency_key_reused": hypothesis.get(
                "idempotency_key_reused"
            ),
        },
        "fact_provenance": hypothesis.get("fact_provenance", {}),
        "execution": {
            "occurred": execution["occurred"],
            "observed_side_effects": execution["observed_side_effects"],
            "attempt_ids": execution["attempt_ids"],
            "side_effect_ids": execution["side_effect_ids"],
            "retry_budget": execution.get("retry_budget"),
            "events": [
                {
                    "seq": event["seq"],
                    "kind": event["kind"],
                    "attempt_id": event["attempt_id"],
                }
                for event in execution["events"]
            ],
        },
        "limitations": report.get("limitations", []),
    }


def _fact(fact: dict[str, Any] | None) -> str:
    if not isinstance(fact, dict):
        return "unavailable"
    status = fact.get("status")
    if status == "known":
        return f"known({fact.get('value')!r})"
    return str(status)