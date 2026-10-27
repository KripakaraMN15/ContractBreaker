"""Reference payment service with configurable idempotency support.

The service records its committed side effects in an observable ledger and
stamps every event with the attempt identifier supplied by the fault adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Flask, jsonify, request

from contractbreaker.events import EventKind, EventLog
from contractbreaker.faults import SUSPENDED_HEADER
from contractbreaker.observations import key_fingerprint


def create_payment_app(
    ledger: list[dict[str, Any]],
    log: EventLog,
    *,
    supports_idempotency: bool,
) -> Flask:
    """Build a payment app whose side effects are appended to ``ledger``.

    When ``supports_idempotency`` is true the server deduplicates requests
    that carry a known ``Idempotency-Key``. A client that omits the key is not
    protected.
    """

    app = Flask(__name__)
    seen_keys: dict[str, dict[str, Any]] = {}

    @app.post("/payments")
    def create_payment() -> tuple[Any, int]:
        body = request.get_json(silent=True) or {}
        attempt_id = request.headers.get("X-Attempt-Id")
        operation_id = request.headers.get("X-Operation-Id")
        idempotency_key = request.headers.get("Idempotency-Key")

        log.record(
            EventKind.REQUEST_RECEIVED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            has_idempotency_key=idempotency_key is not None,
        )

        if (
            supports_idempotency
            and idempotency_key is not None
            and idempotency_key in seen_keys
        ):
            log.record(
                EventKind.IDEMPOTENT_REPLAY,
                attempt_id=attempt_id,
                operation_id=operation_id,
                idempotency_key_fingerprint=key_fingerprint(idempotency_key),
            )
            return jsonify(seen_keys[idempotency_key]), 201

        side_effect_id = f"effect-{len(ledger) + 1}"
        payment: dict[str, Any] = {
            "id": side_effect_id,
            "amount_cents": body.get("amount_cents"),
        }
        ledger.append(payment)
        log.record(
            EventKind.SIDE_EFFECT_COMMITTED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            side_effect_id=side_effect_id,
            amount_cents=payment["amount_cents"],
        )

        if supports_idempotency and idempotency_key is not None:
            seen_keys[idempotency_key] = payment

        return jsonify(payment), 201

    return app


@dataclass
class _SuspendedPayment:
    attempt_id: str | None
    operation_id: str | None
    idempotency_key: str | None
    amount_cents: Any


class TimeoutPaymentService:
    """Payment service that can suspend a request before committing.

    Suspension is deterministic: the service records the receive and then either
    commits immediately or stores the operation so the harness can finish it
    later. No clock, thread, or sleep is involved, so the ordering of the
    resulting events is fixed by configuration rather than by scheduling.
    """

    def __init__(
        self,
        ledger: list[dict[str, Any]],
        log: EventLog,
        *,
        supports_idempotency: bool,
        suspend: bool,
    ) -> None:
        self._ledger = ledger
        self._log = log
        self._supports_idempotency = supports_idempotency
        self._suspend = suspend
        self._seen_keys: dict[str, dict[str, Any]] = {}
        self._pending: _SuspendedPayment | None = None
        self.app = Flask(__name__)
        self.app.add_url_rule(
            "/payments", view_func=self._create_payment, methods=["POST"]
        )

    @property
    def pendable(self) -> bool:
        """Whether this service suspends instead of committing immediately."""

        return self._suspend

    def _create_payment(self) -> Any:
        body = request.get_json(silent=True) or {}
        attempt_id = request.headers.get("X-Attempt-Id")
        operation_id = request.headers.get("X-Operation-Id")
        idempotency_key = request.headers.get("Idempotency-Key")

        self._log.record(
            EventKind.REQUEST_RECEIVED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            has_idempotency_key=idempotency_key is not None,
        )

        if self._suspend:
            self._pending = _SuspendedPayment(
                attempt_id=attempt_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                amount_cents=body.get("amount_cents"),
            )
            return jsonify({"status": "pending"}), 202, {SUSPENDED_HEADER: "1"}

        payment, outcome = self._commit(
            attempt_id, operation_id, idempotency_key, body.get("amount_cents")
        )
        self._log.record(
            EventKind.SERVER_PROCESSING_COMPLETED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            outcome=outcome,
            origin="test_service",
        )
        return jsonify(payment), 201

    def _commit(
        self,
        attempt_id: str | None,
        operation_id: str | None,
        idempotency_key: str | None,
        amount_cents: Any,
    ) -> tuple[dict[str, Any], str]:
        if (
            self._supports_idempotency
            and idempotency_key is not None
            and idempotency_key in self._seen_keys
        ):
            self._log.record(
                EventKind.IDEMPOTENT_REPLAY,
                attempt_id=attempt_id,
                operation_id=operation_id,
                idempotency_key_fingerprint=key_fingerprint(idempotency_key),
            )
            return self._seen_keys[idempotency_key], "replayed"

        side_effect_id = f"effect-{len(self._ledger) + 1}"
        payment: dict[str, Any] = {
            "id": side_effect_id,
            "amount_cents": amount_cents,
        }
        self._ledger.append(payment)
        self._log.record(
            EventKind.SIDE_EFFECT_COMMITTED,
            attempt_id=attempt_id,
            operation_id=operation_id,
            side_effect_id=side_effect_id,
            amount_cents=amount_cents,
        )
        if self._supports_idempotency and idempotency_key is not None:
            self._seen_keys[idempotency_key] = payment
        return payment, "committed"

    def complete(self, *, commit: bool) -> None:
        """Finish the suspended operation, or record that it was aborted."""

        pending = self._pending
        if pending is None:
            return
        self._pending = None
        outcome = "aborted"
        if commit:
            _, outcome = self._commit(
                pending.attempt_id,
                pending.operation_id,
                pending.idempotency_key,
                pending.amount_cents,
            )
        self._log.record(
            EventKind.SERVER_PROCESSING_COMPLETED,
            attempt_id=pending.attempt_id,
            operation_id=pending.operation_id,
            outcome=outcome,
            origin="test_service",
        )
