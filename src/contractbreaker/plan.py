"""Plan-bound approval.

A plan artifact records the exact investigation a human reviewed: the source
path and its content hash, the selected scenario, the execution-relevant
options, and the containment root. Approving that file is only accepted while
every bound field still matches, so an approval can not be replayed onto a
different source, a different scenario, or different options.

This binds approval to *state*, not to time. A plan stays valid for as long as
the source, options, tool version, and root are unchanged; it is not one-shot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PLAN_SCHEMA_VERSION = "1.0"
DIGEST_ALGORITHM = "sha256"

_BOUND_KEYS = (
    "plan_schema_version",
    "tool_version",
    "scenario",
    "source",
    "constraints",
    "allowed_root",
    "runnable",
)


class PlanBindingError(ValueError):
    """Raised when a plan artifact is malformed, altered, or stale."""


def source_digest(path: str | Path) -> str:
    """Return the SHA-256 of the source file exactly as it was reviewed."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_constraints(
    *,
    retry_statuses: tuple[int, ...] | list[int] | None,
    server_idempotency: str | None,
    server_error_status: int | None,
    execution_timeout: float | None,
    llm_fallback: bool,
) -> dict[str, Any]:
    """Normalize the options that change what would be executed."""

    return {
        "retry_statuses": (
            sorted(int(status) for status in retry_statuses)
            if retry_statuses
            else None
        ),
        "server_idempotency": server_idempotency,
        "server_error_status": (
            int(server_error_status)
            if server_error_status is not None
            else None
        ),
        "execution_timeout": (
            float(execution_timeout)
            if execution_timeout is not None
            else None
        ),
        "llm_fallback": bool(llm_fallback),
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _payload(
    *,
    tool_version: str,
    scenario: str,
    source_path: str,
    source_sha256: str,
    constraints: dict[str, Any],
    allowed_root: str | None,
    runnable: bool,
) -> dict[str, Any]:
    return {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "tool_version": tool_version,
        "scenario": scenario,
        "source": {"path": source_path, "sha256": source_sha256},
        "constraints": constraints,
        "allowed_root": allowed_root,
        "runnable": bool(runnable),
    }


def build_plan(
    *,
    tool_version: str,
    scenario: str,
    scenario_source: str | None,
    source_path: str | Path,
    source_sha256: str,
    constraints: dict[str, Any],
    allowed_root: str | Path | None,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    """Build a plan artifact for human review and later approval."""

    resolved_root = str(allowed_root) if allowed_root is not None else None
    payload = _payload(
        tool_version=tool_version,
        scenario=scenario,
        source_path=str(source_path),
        source_sha256=source_sha256,
        constraints=constraints,
        allowed_root=resolved_root,
        runnable=bool(readiness.get("runnable")),
    )
    return {
        **payload,
        "scenario_source": scenario_source,
        "readiness": {
            "scenario": readiness.get("scenario"),
            "runnable": bool(readiness.get("runnable")),
            "blockers": list(readiness.get("blockers") or []),
        },
        "binding": {
            "algorithm": DIGEST_ALGORITHM,
            "digest": _digest(payload),
        },
    }


def write_plan(path: str | Path, plan: dict[str, Any]) -> None:
    """Write a plan artifact deterministically."""

    Path(path).write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _payload_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    source = plan.get("source")
    if not isinstance(source, dict):
        raise PlanBindingError("plan has no source block")
    constraints = plan.get("constraints")
    if not isinstance(constraints, dict):
        raise PlanBindingError("plan has no constraints block")
    return _payload(
        tool_version=plan.get("tool_version"),
        scenario=plan.get("scenario"),
        source_path=source.get("path"),
        source_sha256=source.get("sha256"),
        constraints=constraints,
        allowed_root=plan.get("allowed_root"),
        runnable=plan.get("runnable"),
    )


def load_plan(path: str | Path) -> dict[str, Any]:
    """Load a plan artifact and verify it was not modified after creation."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanBindingError(f"plan file could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanBindingError("plan file is not a JSON object")
    if raw.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanBindingError(
            "plan schema version is not supported: "
            f"{raw.get('plan_schema_version')!r}"
        )
    binding = raw.get("binding")
    if (
        not isinstance(binding, dict)
        or binding.get("algorithm") != DIGEST_ALGORITHM
        or not isinstance(binding.get("digest"), str)
    ):
        raise PlanBindingError("plan has no usable binding block")
    expected = _digest(_payload_from_plan(raw))
    if expected != binding["digest"]:
        raise PlanBindingError(
            "plan contents do not match their digest; the plan was modified "
            "after it was created"
        )
    return raw


def verify_plan(
    plan: dict[str, Any],
    *,
    tool_version: str,
    scenario: str,
    source_path: str | Path,
    source_sha256: str,
    constraints: dict[str, Any],
    allowed_root: str | Path | None,
) -> None:
    """Verify a loaded plan against the investigation about to run.

    Every mismatch is collected so the caller can report all of them at once.
    """

    if plan.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanBindingError(
            "plan schema version is not supported: "
            f"{plan.get('plan_schema_version')!r}"
        )

    expected = _payload(
        tool_version=tool_version,
        scenario=scenario,
        source_path=str(source_path),
        source_sha256=source_sha256,
        constraints=constraints,
        allowed_root=(
            str(allowed_root) if allowed_root is not None else None
        ),
        runnable=plan.get("runnable"),
    )

    reasons: list[str] = []
    if plan.get("tool_version") != expected["tool_version"]:
        reasons.append(
            "the plan was created by a different tool version "
            f"({plan.get('tool_version')!r} != {expected['tool_version']!r})"
        )
    if plan.get("scenario") != expected["scenario"]:
        reasons.append(
            "the scenario differs from the one that was reviewed "
            f"({plan.get('scenario')!r} != {expected['scenario']!r})"
        )
    planned_source = plan.get("source") or {}
    if planned_source.get("path") != expected["source"]["path"]:
        reasons.append("the source path differs from the plan")
    if planned_source.get("sha256") != expected["source"]["sha256"]:
        reasons.append(
            "the source content changed after the plan was created"
        )
    planned_constraints = plan.get("constraints") or {}
    for name in expected["constraints"]:
        if planned_constraints.get(name) != expected["constraints"][name]:
            reasons.append(f"the option {name!r} differs from the plan")
    if plan.get("allowed_root") != expected["allowed_root"]:
        reasons.append("the allowed root differs from the plan")
    if plan.get("runnable") is not True:
        reasons.append(
            "the plan was not runnable, so it can not be approved"
        )

    if reasons:
        raise PlanBindingError("; ".join(dict.fromkeys(reasons)))
