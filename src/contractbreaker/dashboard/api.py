"""Typed backend adapter for the ContractBreaker dashboard.

The dashboard never reimplements analysis logic. It validates input, calls
:func:`contractbreaker.workflow.run_workflow`, and returns the workflow's own
documents. Execution approval is enforced here, on the server: a plan is
required, it must be runnable, and it is consumed at most once.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..workflow import (
    SUPPORTED_WORKFLOW_SCENARIOS,
    WorkflowRequest,
    run_workflow,
)

API_SCHEMA_VERSION = "1.0"
MAX_REQUEST_LENGTH = 2000
MAX_SOURCES_LISTED = 200
_ALLOWED_SUFFIX = ".py"
_SERVER_IDEMPOTENCY = ("auto", "on", "off")


class ApiError(Exception):
    """A client-facing error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass
class _Approval:
    """A server-side approval record bound to one concrete plan."""

    token: str
    request: str
    source: str
    scenario: str
    constraints: dict[str, Any]
    llm_fallback: bool
    consumed: bool = False
    created_at: float = field(default_factory=os.times)


@dataclass(frozen=True)
class DashboardConfig:
    """Server configuration. Only non-sensitive values are exposed."""

    allowed_root: Path
    llm_fallback_available: bool = False

    @property
    def allowed_root_text(self) -> str:
        return str(self.allowed_root)


class DashboardService:
    """Validates requests and runs the existing ContractBreaker workflow."""

    def __init__(self, config: DashboardConfig) -> None:
        self._config = config
        self._approvals: dict[str, _Approval] = {}

    # -- public API ----------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return non-sensitive service metadata."""

        return {
            "ok": True,
            "api_schema_version": API_SCHEMA_VERSION,
            "allowed_root": self._config.allowed_root_text,
            "llm_fallback_available": self._config.llm_fallback_available,
            "supported_scenarios": list(SUPPORTED_WORKFLOW_SCENARIOS),
        }

    def list_sources(self) -> dict[str, Any]:
        """List analyzable Python files under the allowed root."""

        root = self._config.allowed_root
        if not root.is_dir():
            raise ApiError(
                "allowed_root_unavailable",
                "the configured source root is not a directory",
                status=503,
            )
        found: list[str] = []
        for path in sorted(root.rglob(f"*{_ALLOWED_SUFFIX}")):
            if len(found) >= MAX_SOURCES_LISTED:
                break
            if any(
                part in {".venv", ".git", "__pycache__", "node_modules"}
                for part in path.parts
            ):
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            found.append(str(relative))
        return {"ok": True, "sources": found}

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Produce a read-only plan. This never executes the analyzed client."""

        request = _validate_request(payload)
        source = self._validate_source(payload.get("source"))
        scenario = _validate_scenario(payload.get("scenario"))
        constraints = _validate_constraints(payload, scenario)
        llm_fallback = _validate_bool(payload.get("llm_fallback"), "llm_fallback")

        state = run_workflow(
            WorkflowRequest(
                request=request,
                source=source,
                scenario=scenario,
                retry_statuses=constraints.get("retry_statuses"),
                server_idempotency=constraints.get("server_idempotency"),
                server_error_status=constraints.get("server_error_status"),
                execution_timeout=constraints.get("execution_timeout"),
                llm_fallback=llm_fallback,
                plan_only=True,
            )
        )
        document = state.snapshot()
        readiness = _readiness_of(document)
        ready = bool(readiness["runnable"])
        approval_token = None
        if ready:
            approval_token = self._issue_approval(
                request=request,
                source=str(source),
                scenario=str(document["request"]["scenario"]),
                constraints=constraints,
                llm_fallback=llm_fallback,
            )
        return {
            "ok": True,
            "api_schema_version": API_SCHEMA_VERSION,
            "ready": ready,
            "approval_token": approval_token,
            "plan": document,
        }

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a previously approved, still-runnable plan exactly once."""

        token = payload.get("approval_token")
        if not isinstance(token, str) or not token.strip():
            raise ApiError(
                "approval_required",
                "an approval_token from a successful plan is required",
                status=403,
            )
        if payload.get("approved") is not True:
            raise ApiError(
                "approval_required",
                "execution must be explicitly approved",
                status=403,
            )
        approval = self._approvals.get(token)
        if approval is None:
            raise ApiError(
                "unknown_approval",
                "the approval token is unknown or already consumed",
                status=409,
            )
        if approval.consumed:
            raise ApiError(
                "already_executed",
                "this plan was already executed; create a new plan to retry",
                status=409,
            )

        # Re-validate readiness on the server. The client state is never
        # trusted to decide whether execution is allowed.
        source = self._validate_source(approval.source)
        state = run_workflow(
            WorkflowRequest(
                request=approval.request,
                source=source,
                scenario=approval.scenario,
                retry_statuses=approval.constraints.get("retry_statuses"),
                server_idempotency=approval.constraints.get(
                    "server_idempotency"
                ),
                server_error_status=approval.constraints.get(
                    "server_error_status"
                ),
                execution_timeout=approval.constraints.get("execution_timeout"),
                llm_fallback=approval.llm_fallback,
                approved=True,
            )
        )
        document = state.snapshot()
        readiness = _readiness_of(document)
        if not readiness["runnable"]:
            approval.consumed = True
            raise ApiError(
                "not_runnable",
                "the client is no longer runnable for this scenario",
                status=409,
            )
        approval.consumed = True
        return {
            "ok": True,
            "api_schema_version": API_SCHEMA_VERSION,
            "result": document,
        }

    # -- internals -----------------------------------------------------

    def _issue_approval(
        self,
        *,
        request: str,
        source: str,
        scenario: str,
        constraints: dict[str, Any],
        llm_fallback: bool,
    ) -> str:
        token = secrets.token_urlsafe(24)
        self._approvals[token] = _Approval(
            token=token,
            request=request,
            source=source,
            scenario=scenario,
            constraints=constraints,
            llm_fallback=llm_fallback,
        )
        return token

    def _validate_source(self, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ApiError(
                "invalid_source", "a source file path is required", status=400
            )
        if len(value) > 1024:
            raise ApiError("invalid_source", "source path is too long")
        if "\x00" in value:
            raise ApiError("invalid_source", "source path contains invalid characters")
        root = self._config.allowed_root.resolve()
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate
        else:
            resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ApiError(
                "invalid_source",
                "source path must stay inside the allowed source root",
                status=403,
            ) from None
        if resolved.suffix != _ALLOWED_SUFFIX:
            raise ApiError(
                "invalid_source", "source file must be a Python .py file"
            )
        if not resolved.is_file():
            raise ApiError("invalid_source", "source file does not exist")
        return resolved


def _readiness_of(document: dict[str, Any]) -> dict[str, Any]:
    """Return the scenario readiness of a workflow document.

    A request that was blocked during interpretation has no analysis
    snapshot; it is reported as not runnable with its own blockers.
    """

    snapshot = document.get("analysis_snapshot")
    if not snapshot:
        blockers = []
        for stage in document.get("stages", []):
            if stage.get("outcome") == "blocked":
                blockers.extend(stage.get("blockers", []))
        return {
            "scenario": document.get("request", {}).get("scenario"),
            "runnable": False,
            "blockers": blockers,
            "unknowns": [],
        }
    return snapshot["hypothesis"]["scenario_readiness"]


def _validate_request(payload: dict[str, Any]) -> str:
    value = payload.get("request")
    if value is None and payload.get("scenario") and payload.get("source"):
        return ""
    if not isinstance(value, str):
        raise ApiError("invalid_request", "request must be a string")
    request = " ".join(value.split())
    if not request:
        raise ApiError(
            "invalid_request",
            "provide a request, or both scenario and source",
        )
    if len(request) > MAX_REQUEST_LENGTH:
        raise ApiError(
            "invalid_request",
            f"request must be at most {MAX_REQUEST_LENGTH} characters",
        )
    return request


def _validate_scenario(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError("invalid_scenario", "scenario must be a string")
    if value not in SUPPORTED_WORKFLOW_SCENARIOS:
        raise ApiError(
            "invalid_scenario",
            "scenario must be one of: "
            + ", ".join(SUPPORTED_WORKFLOW_SCENARIOS),
        )
    return value


def _validate_bool(value: object, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ApiError("invalid_input", f"{field} must be a boolean")
    return value


def _validate_constraints(
    payload: dict[str, Any], scenario: str | None
) -> dict[str, Any]:
    constraints: dict[str, Any] = {}

    server_idempotency = payload.get("server_idempotency")
    if server_idempotency not in (None, "", *_SERVER_IDEMPOTENCY):
        raise ApiError(
            "invalid_constraint",
            "server_idempotency must be auto, on, or off",
        )
    if server_idempotency:
        constraints["server_idempotency"] = server_idempotency

    retry_statuses = payload.get("retry_statuses")
    if retry_statuses is not None:
        if scenario != "retryable-error":
            raise ApiError(
                "invalid_constraint",
                "retry_statuses require the retryable-error scenario",
            )
        if not isinstance(retry_statuses, list) or not retry_statuses:
            raise ApiError(
                "invalid_constraint",
                "retry_statuses must be a non-empty list",
            )
        statuses: list[int] = []
        for item in retry_statuses:
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 400 <= item <= 599
            ):
                raise ApiError(
                    "invalid_constraint",
                    "retry statuses must be HTTP error codes between 400 and 599",
                )
            statuses.append(item)
        constraints["retry_statuses"] = sorted(set(statuses))

    server_error_status = payload.get("server_error_status")
    if server_error_status is not None:
        if scenario != "retryable-error":
            raise ApiError(
                "invalid_constraint",
                "server_error_status requires the retryable-error scenario",
            )
        if (
            isinstance(server_error_status, bool)
            or not isinstance(server_error_status, int)
            or not 400 <= server_error_status <= 599
        ):
            raise ApiError(
                "invalid_constraint",
                "server_error_status must be an HTTP error code",
            )
        constraints["server_error_status"] = server_error_status

    execution_timeout = payload.get("execution_timeout")
    if execution_timeout is not None:
        if (
            isinstance(execution_timeout, bool)
            or not isinstance(execution_timeout, (int, float))
            or execution_timeout <= 0
            or execution_timeout > 300
        ):
            raise ApiError(
                "invalid_constraint",
                "execution_timeout must be a positive number up to 300 seconds",
            )
        constraints["execution_timeout"] = float(execution_timeout)

    return constraints
