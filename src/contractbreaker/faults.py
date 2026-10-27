"""Deterministic response-loss injection for in-process HTTP calls.

This adapter is intentionally not a network proxy. It routes ``requests``
calls into a Flask test client, lets the server finish, and then drops the
response for the selected attempt. That models "the commit succeeded but the
client saw a timeout" without sockets, sleeps, or random scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter

from .events import EventKind, EventLog
from .observations import key_fingerprint

TIMEOUT_MODES = (
    "server_first",
    "client_first_then_commit",
    "client_first_no_commit",
)
SUSPENDED_HEADER = "X-Harness-Suspended"


def request_path(request: requests.PreparedRequest) -> str:
    """Return the request path with any query string removed."""

    return urlsplit(request.path_url or "/").path or "/"


@dataclass(frozen=True)
class FaultPlan:
    """Which 1-based HTTP attempts should lose their response.

    Attempt numbers are counted only for requests whose path matches
    ``target_path``. An unrelated request can therefore never consume an
    attempt number and shift which request loses its response.

    ``timeout_mode`` enables the simulated client-timeout scenario. The timeout
    is injected by this harness at a chosen ordering point; it is never a real
    socket or ``requests`` timeout.
    """

    drop_response_on_attempts: frozenset[int] = field(
        default_factory=lambda: frozenset({1})
    )
    target_path: str = "/payments"
    timeout_mode: str | None = None
    declared_timeout_seconds: float | None = None
    declared_timeout_source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.drop_response_on_attempts, frozenset):
            object.__setattr__(
                self,
                "drop_response_on_attempts",
                frozenset(self.drop_response_on_attempts),
            )
        if any(attempt < 1 for attempt in self.drop_response_on_attempts):
            raise ValueError("attempt numbers are 1-based and must be positive")
        if not isinstance(self.target_path, str) or not self.target_path.startswith("/"):
            raise ValueError("target_path must be an absolute request path")
        if self.timeout_mode is not None:
            if self.timeout_mode not in TIMEOUT_MODES:
                raise ValueError(f"unknown timeout mode: {self.timeout_mode!r}")
            seconds = self.declared_timeout_seconds
            if (
                not isinstance(seconds, (int, float))
                or isinstance(seconds, bool)
                or seconds <= 0
            ):
                raise ValueError(
                    "a timeout mode requires a positive declared timeout"
                )


class FaultInjectionAdapter(BaseAdapter):
    """Send HTTP requests to a WSGI app and drop planned responses."""

    def __init__(self, app: object, log: EventLog, plan: FaultPlan) -> None:
        super().__init__()
        self._client = app.test_client()  # type: ignore[attr-defined]
        self._log = log
        self._plan = plan
        self._target_attempt_number = 0
        self._unrelated_attempt_number = 0

    def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Execute one attempt and raise a timeout when its response is lost.

        Only requests that target the plan's path consume an attempt number.
        Anything else is labelled as unrelated so it can neither trigger a
        drop nor be mistaken for a payment attempt.
        """

        path = request_path(request)
        targets_plan = path == self._plan.target_path
        if targets_plan:
            self._target_attempt_number += 1
            attempt_number: int | None = self._target_attempt_number
            attempt_id = f"attempt-{attempt_number}"
        else:
            self._unrelated_attempt_number += 1
            attempt_number = None
            attempt_id = f"unrelated-{self._unrelated_attempt_number}"
        operation_id = request.headers.get("X-Operation-Id")
        key_value = request.headers.get("Idempotency-Key")

        # Only the key's presence and a non-reversible fingerprint are recorded.
        # Request credentials and other headers never enter the event trace.
        timeout_context: dict[str, object] = {}
        if self._plan.declared_timeout_seconds is not None:
            timeout_context = {
                "declared_timeout_seconds": self._plan.declared_timeout_seconds,
                "declared_timeout_source": self._plan.declared_timeout_source,
            }
        self._log.record(
            EventKind.REQUEST_SENT,
            attempt_id=attempt_id,
            operation_id=operation_id,
            method=request.method,
            path=path,
            idempotency_key_present=key_value is not None,
            idempotency_key_fingerprint=(
                key_fingerprint(key_value) if key_value is not None else None
            ),
            **timeout_context,
        )

        # The attempt identifier travels with the request so server events can
        # be attributed to this exact attempt.
        headers = dict(request.headers)
        headers["X-Attempt-Id"] = attempt_id
        server_response = self._client.open(
            path=request.path_url,
            method=request.method,
            headers=headers,
            data=request.body,
        )

        if (
            targets_plan
            and self._plan.timeout_mode is not None
            and server_response.headers.get(SUSPENDED_HEADER) == "1"
        ):
            # The harness injects this timeout at a chosen ordering point. The
            # client really does observe a ReadTimeout, but ``requests``' own
            # timeout parameter is not what produces it; the provenance fields
            # below exist so the report can never imply otherwise.
            self._log.record(
                EventKind.CLIENT_TIMEOUT_OBSERVED,
                attempt_id=attempt_id,
                operation_id=operation_id,
                declared_timeout_seconds=self._plan.declared_timeout_seconds,
                declared_timeout_source=self._plan.declared_timeout_source,
                enforced_by="harness",
                timeout_transport="in_process_wsgi",
                origin="harness_timeout_fault",
                real_requests_timeout_enforced=False,
                timeout_mode=self._plan.timeout_mode,
            )
            raise requests.exceptions.ReadTimeout(
                f"simulated client timeout for {attempt_id}", request=request
            )

        if (
            attempt_number is not None
            and attempt_number in self._plan.drop_response_on_attempts
        ):
            self._log.record(
                EventKind.RESPONSE_DROPPED,
                attempt_id=attempt_id,
                operation_id=operation_id,
                status_code=server_response.status_code,
            )
            raise requests.exceptions.ReadTimeout(
                f"simulated lost response for {attempt_id}", request=request
            )

        self._log.record(
            EventKind.RESPONSE_RETURNED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            status_code=server_response.status_code,
        )
        return self._to_requests_response(request, server_response)

    @staticmethod
    def _to_requests_response(
        request: requests.PreparedRequest, server_response: object
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = server_response.status_code  # type: ignore[attr-defined]
        response._content = server_response.data  # type: ignore[attr-defined]
        response.headers.update(server_response.headers)  # type: ignore[attr-defined]
        response.url = request.url or ""
        response.request = request
        response.reason = server_response.status  # type: ignore[attr-defined]
        return response

    def close(self) -> None:
        """Required by ``BaseAdapter``; the in-process client holds no sockets."""

        self._client = None  # type: ignore[assignment]