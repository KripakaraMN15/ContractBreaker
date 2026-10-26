"""Deterministic response-loss injection for in-process HTTP calls.

This adapter is intentionally not a network proxy. It routes ``requests``
calls into a Flask test client, lets the server finish, and then drops the
response for the selected attempt. That models "the commit succeeded but the
client saw a timeout" without sockets, sleeps, or random scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests
from requests.adapters import BaseAdapter

from .events import EventKind, EventLog


@dataclass(frozen=True)
class FaultPlan:
    """Which 1-based HTTP attempts should lose their response."""

    drop_response_on_attempts: frozenset[int] = field(
        default_factory=lambda: frozenset({1})
    )

    def __post_init__(self) -> None:
        if not isinstance(self.drop_response_on_attempts, frozenset):
            object.__setattr__(
                self,
                "drop_response_on_attempts",
                frozenset(self.drop_response_on_attempts),
            )
        if any(attempt < 1 for attempt in self.drop_response_on_attempts):
            raise ValueError("attempt numbers are 1-based and must be positive")


class FaultInjectionAdapter(BaseAdapter):
    """Send HTTP requests to a WSGI app and drop planned responses."""

    def __init__(self, app: object, log: EventLog, plan: FaultPlan) -> None:
        super().__init__()
        self._client = app.test_client()  # type: ignore[attr-defined]
        self._log = log
        self._plan = plan
        self._attempt_number = 0

    def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        """Execute one attempt and raise a timeout when its response is lost."""

        self._attempt_number += 1
        attempt_number = self._attempt_number
        attempt_id = f"attempt-{attempt_number}"
        operation_id = request.headers.get("X-Operation-Id")

        self._log.record(
            EventKind.REQUEST_SENT,
            attempt_id=attempt_id,
            operation_id=operation_id,
            method=request.method,
            path=request.path_url,
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

        if attempt_number in self._plan.drop_response_on_attempts:
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