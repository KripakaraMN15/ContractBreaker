"""Payment client that retries on timeout. `use_idempotency_key=False` is the
seeded bug: a retried POST is indistinguishable from a new payment."""

import uuid

import requests

BASE_URL = "http://service.test"


class PaymentClient:
    def __init__(self, session: requests.Session, use_idempotency_key: bool = False,
                 max_retries: int = 2, timeout: float = 5.0) -> None:
        self._session = session
        self._use_key = use_idempotency_key
        self._max_retries = max_retries
        self._timeout = timeout

    def create_payment(self, amount_cents: int) -> dict:
        headers = {"Idempotency-Key": str(uuid.uuid4())} if self._use_key else {}
        last_exc: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                resp = self._session.post(
                    f"{BASE_URL}/payments",
                    json={"amount_cents": amount_cents},
                    headers=headers,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]
