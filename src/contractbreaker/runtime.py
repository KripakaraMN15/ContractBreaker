"""Parent-side launcher for isolated analyzed-client execution."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .events import Event, EventKind


class RuntimeExecutionError(RuntimeError):
    """Raised when the isolated client execution can not be trusted."""


@dataclass(frozen=True)
class RuntimeRunResult:
    """Observed events and side-effect count from the worker."""

    events: tuple[Event, ...]
    observed_side_effects: int


def run_analyzed_client(
    *,
    source: str | Path,
    scenario: str,
    server_supports_idempotency: bool,
    retry_statuses: tuple[int, ...] = (),
    server_error_status: int = 503,
    policy_max_retries: int = 1,
    execution_timeout_seconds: float = 10.0,
) -> RuntimeRunResult:
    """Run the analyzed module in a subprocess and read its event trace."""

    source_path = Path(source).resolve()
    with tempfile.TemporaryDirectory(prefix="contractbreaker-runtime-") as tmp:
        temporary = Path(tmp)
        result_path = temporary / "result.json"
        command = [
            sys.executable,
            "-m",
            "contractbreaker.runtime_worker",
            "--source",
            str(source_path),
            "--result",
            str(result_path),
            "--scenario",
            scenario,
            "--server-error-status",
            str(server_error_status),
            "--policy-max-retries",
            str(policy_max_retries),
        ]
        if server_supports_idempotency:
            command.append("--server-idempotency")
        for status in retry_statuses:
            command.extend(["--retry-status", str(status)])

        try:
            completed = subprocess.run(
                command,
                cwd=temporary,
                timeout=execution_timeout_seconds,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeExecutionError(
                f"runtime execution timed out after "
                f"{execution_timeout_seconds:.1f}s"
            ) from exc

        if not result_path.is_file():
            detail = (completed.stderr or "").strip()
            suffix = f": {detail[:500]}" if detail else ""
            raise RuntimeExecutionError(
                "runtime worker produced no result "
                f"(exit {completed.returncode}){suffix}"
            )

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeExecutionError(
                "runtime worker result could not be parsed"
            ) from exc

        worker_error = payload.get("worker_error")
        if worker_error:
            raise RuntimeExecutionError(f"runtime worker failed: {worker_error}")

        try:
            events = tuple(
                Event(
                    seq=item["seq"],
                    kind=EventKind(item["kind"]),
                    attempt_id=item.get("attempt_id"),
                    operation_id=item.get("operation_id"),
                    data=item.get("data") or {},
                )
                for item in payload.get("events", [])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeExecutionError(
                "runtime event trace is malformed"
            ) from exc

        observed = payload.get("observed_side_effects")
        if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
            raise RuntimeExecutionError(
                "runtime worker reported an invalid side-effect count"
            )
        return RuntimeRunResult(events=events, observed_side_effects=observed)