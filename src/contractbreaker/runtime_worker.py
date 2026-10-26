"""Subprocess worker that executes one supported analyzed client source file.

The worker runs outside the CLI process. It routes all ``requests`` traffic to
an in-process Flask test service, blocks socket construction, records attempt-
and operation-aware events, and writes a JSON trace for the parent verifier.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import runpy
import socket
import uuid
from pathlib import Path
from typing import Any, Sequence

import requests

from examples.payments.retryable_error_service import (
    create_retryable_error_payment_app,
)
from examples.payments.service import create_payment_app

from .events import EventKind, EventLog
from .faults import FaultInjectionAdapter, FaultPlan

_ORIGINAL_SESSION = requests.Session


class RuntimeEventLog(EventLog):
    """Event log that converts a retryable response into an observed decision."""

    def __init__(self, retry_statuses: frozenset[int]) -> None:
        super().__init__()
        self._retry_statuses = retry_statuses
        self._pending: dict[str, tuple[str, int]] = {}

    def record(
        self,
        kind: EventKind,
        *,
        attempt_id: str | None = None,
        operation_id: str | None = None,
        **data: Any,
    ):
        if (
            kind is EventKind.REQUEST_SENT
            and operation_id is not None
            and operation_id in self._pending
        ):
            previous_attempt, status_code = self._pending.pop(operation_id)
            super().record(
                EventKind.RETRY_SCHEDULED,
                attempt_id=previous_attempt,
                operation_id=operation_id,
                status_code=status_code,
                retry_statuses=tuple(sorted(self._retry_statuses)),
            )
        event = super().record(
            kind,
            attempt_id=attempt_id,
            operation_id=operation_id,
            **data,
        )
        if (
            kind is EventKind.RESPONSE_RETURNED
            and attempt_id is not None
            and operation_id is not None
            and data.get("status_code") in self._retry_statuses
        ):
            self._pending[operation_id] = (attempt_id, data["status_code"])
        return event


class RuntimeFaultAdapter(FaultInjectionAdapter):
    """Inject harness metadata and keep a shared test-client transport open."""

    def __init__(
        self,
        app: object,
        log: EventLog,
        plan: FaultPlan,
        *,
        operation_id: str,
    ) -> None:
        super().__init__(app, log, plan)
        self._operation_id = operation_id

    def send(self, request: requests.PreparedRequest, **kwargs: object):
        if not request.headers.get("X-Operation-Id"):
            request.headers["X-Operation-Id"] = self._operation_id
        return super().send(request, **kwargs)

    def close(self) -> None:
        # The worker owns this adapter for the whole source execution. Source
        # code may close a Session; that must not close the shared test client.
        pass


def _make_runtime_session_class(adapter: RuntimeFaultAdapter):
    class RuntimeSession(_ORIGINAL_SESSION):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.mount("http://", adapter)
            self.mount("https://", adapter)

    return RuntimeSession


def _patch_requests(adapter: RuntimeFaultAdapter) -> None:
    session_class = _make_runtime_session_class(adapter)
    requests.Session = session_class
    requests.sessions.Session = session_class


def _block_network() -> None:
    class BlockedSocket:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("network access is disabled during runtime verification")

    def blocked_connection(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access is disabled during runtime verification")

    socket.socket = BlockedSocket  # type: ignore[assignment]
    socket.create_connection = blocked_connection  # type: ignore[assignment]


def _event_dict(event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind.value,
        "attempt_id": event.attempt_id,
        "operation_id": event.operation_id,
        "data": event.data,
    }


def _write_result(
    path: Path,
    events: Sequence[Any],
    observed_side_effects: int,
    worker_error: str | None = None,
) -> None:
    payload = {
        "events": [_event_dict(event) for event in events],
        "observed_side_effects": observed_side_effects,
        "worker_error": worker_error,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run(args: argparse.Namespace) -> None:
    retry_statuses = frozenset(args.retry_statuses)
    log = RuntimeEventLog(retry_statuses)
    ledger: list[dict[str, object]] = []

    if args.scenario == "retryable-error":
        log.record(
            EventKind.RETRY_POLICY_CONFIGURED,
            retry_statuses=tuple(sorted(retry_statuses)),
            max_retries=args.policy_max_retries,
        )

    if args.scenario == "lost-response":
        app = create_payment_app(
            ledger,
            log,
            supports_idempotency=args.server_idempotency,
        )
        plan = FaultPlan(drop_response_on_attempts=frozenset({1}))
    else:
        app = create_retryable_error_payment_app(
            ledger,
            log,
            supports_idempotency=args.server_idempotency,
            retryable_status=args.server_error_status,
        )
        plan = FaultPlan(drop_response_on_attempts=frozenset())

    adapter = RuntimeFaultAdapter(
        app,
        log,
        plan,
        operation_id=f"runtime-op-{uuid.uuid4()}",
    )
    _patch_requests(adapter)
    _block_network()

    try:
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(
                devnull
            ):
                runpy.run_path(args.source, run_name="__main__")
    except BaseException as exc:
        log.record(
            EventKind.SCENARIO_END,
            outcome="client_gave_up",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
        )
    else:
        log.record(EventKind.SCENARIO_END, outcome="client_completed")

    _write_result(
        Path(args.result),
        log.events,
        observed_side_effects=len(ledger),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=("lost-response", "retryable-error"),
    )
    parser.add_argument("--server-idempotency", action="store_true")
    parser.add_argument(
        "--retry-status",
        dest="retry_statuses",
        action="append",
        type=int,
        default=[],
    )
    parser.add_argument("--server-error-status", type=int, default=503)
    parser.add_argument("--policy-max-retries", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the worker and always attempt to write a structured result."""

    args = build_parser().parse_args(argv)
    try:
        _run(args)
    except BaseException as exc:
        try:
            _write_result(
                Path(args.result),
                (),
                observed_side_effects=0,
                worker_error=f"{type(exc).__name__}: {exc}",
            )
        except OSError:
            return 1
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())