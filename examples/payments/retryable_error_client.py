"""Payment client with an explicit, inspectable retry policy for HTTP errors."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import requests

from contractbreaker.events import EventKind, EventLog

BASE_URL = "http://service.test"


class RetryableErrorClientError(RuntimeError):
    """Raised when the client gives up after a retryable HTTP error."""


@dataclass(frozen=True)
class RetryPolicy:
    """The exact statuses and retry budget the client is allowed to use."""

    retry_statuses: frozenset[int] = frozenset({503})
    max_retries: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.retry_statuses, frozenset):
            object.__setattr__(
                self,
                "retry_statuses",
                frozenset(self.retry_statuses),
            )
        if any(
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
            for status in self.retry_statuses
        ):
            raise ValueError("retry_statuses must contain HTTP error statuses")
        if not isinstance(self.max_retries, int) or isinstance(
            self.max_retries, bool
        ):
            raise ValueError("max_retries must be an integer")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")


class RetryableErrorPaymentClient:
    """Retry only statuses named in the configured policy.

    The policy is recorded in the event log before the first request, and each
    retry decision is recorded with the responding attempt id. This prevents a
    verifier from assuming that an arbitrary 5xx response is retried.
    """

    def __init__(
        self,
        session: requests.Session,
        log: EventLog,
        *,
        policy: RetryPolicy,
        send_idempotency_key: bool,
        timeout: float = 5.0,
    ) -> None:
        self._session = session
        self._log = log
        self._policy = policy
        self._send_idempotency_key = send_idempotency_key
        self._timeout = timeout
        log.record(
            EventKind.RETRY_POLICY_CONFIGURED,
            retry_statuses=tuple(sorted(policy.retry_statuses)),
            max_retries=policy.max_retries,
        )

    def create_payment(self, amount_cents: int) -> dict[str, object]:
        """Post one payment, retrying only explicitly configured statuses."""

        operation_id = f"op-{uuid.uuid4()}"
        idempotency_key = (
            f"key-{uuid.uuid4()}"
            if self._send_idempotency_key
            else None
        )
        last_status: int | None = None

        for attempt_index in range(self._policy.max_retries + 1):
            headers = {"X-Operation-Id": operation_id}
            if idempotency_key is not None:
                headers["Idempotency-Key"] = idempotency_key

            response = self._session.post(
                f"{BASE_URL}/payments",
                json={"amount_cents": amount_cents},
                headers=headers,
                timeout=self._timeout,
            )
            attempt_id = response.headers.get("X-Attempt-Id")
            last_status = response.status_code

            if (
                response.status_code in self._policy.retry_statuses
                and attempt_index < self._policy.max_retries
            ):
                self._log.record(
                    EventKind.RETRY_SCHEDULED,
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    status_code=response.status_code,
                    retry_statuses=tuple(
                        sorted(self._policy.retry_statuses)
                    ),
                    next_attempt=attempt_index + 2,
                )
                continue

            if response.status_code >= 400:
                raise RetryableErrorClientError(
                    f"payment failed with HTTP {response.status_code}"
                )

            payload = response.json()
            if not isinstance(payload, dict):
                raise RetryableErrorClientError(
                    "payment response was not a JSON object"
                )
            return payload

        raise RetryableErrorClientError(
            f"payment failed after status {last_status}"
        )