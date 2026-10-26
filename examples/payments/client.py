"""Reference HTTP client that retries timeouts.

The client always sends a stable logical operation id. It sends an
idempotency key only when configured to do so, which lets the harness isolate
client-side and server-side protection independently.
"""

from __future__ import annotations

import uuid

import requests

BASE_URL = "http://service.test"


class PaymentClientError(RuntimeError):
    """Raised when the client exhausts its retry budget."""


class PaymentClient:
    """Create a payment, retrying the same logical operation after a timeout."""

    def __init__(
        self,
        session: requests.Session,
        *,
        send_idempotency_key: bool,
        max_retries: int = 1,
        timeout: float = 5.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._session = session
        self._send_idempotency_key = send_idempotency_key
        self._max_retries = max_retries
        self._timeout = timeout

    def create_payment(self, amount_cents: int) -> dict[str, object]:
        """Post one payment and retry the same operation on a timeout.

        The operation id and idempotency key are created once, outside the
        retry loop, so every attempt represents the same logical operation.
        """

        operation_id = f"op-{uuid.uuid4()}"
        idempotency_key = (
            f"key-{uuid.uuid4()}" if self._send_idempotency_key else None
        )
        last_error: requests.exceptions.Timeout | None = None

        for _ in range(self._max_retries + 1):
            headers = {"X-Operation-Id": operation_id}
            if idempotency_key is not None:
                headers["Idempotency-Key"] = idempotency_key

            try:
                response = self._session.post(
                    f"{BASE_URL}/payments",
                    json={"amount_cents": amount_cents},
                    headers=headers,
                    timeout=self._timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise PaymentClientError("payment response was not a JSON object")
                return payload
            except requests.exceptions.Timeout as exc:
                last_error = exc

        raise PaymentClientError(
            f"payment failed after {self._max_retries + 1} attempt(s)"
        ) from last_error