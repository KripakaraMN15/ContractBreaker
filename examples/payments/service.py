"""Reference payment service with configurable idempotency support.

The service records its committed side effects in an observable ledger and
stamps every event with the attempt identifier supplied by the fault adapter.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from contractbreaker.events import EventKind, EventLog


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
                idempotency_key=idempotency_key,
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