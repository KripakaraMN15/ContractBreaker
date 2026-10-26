"""Payment service that commits and then returns one retryable HTTP error."""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from contractbreaker.events import EventKind, EventLog


def create_retryable_error_payment_app(
    ledger: list[dict[str, Any]],
    log: EventLog,
    *,
    supports_idempotency: bool,
    retryable_status: int = 503,
) -> Flask:
    """Build a payment app that fails the first attempt of each operation.

    The side effect is committed before the retryable error is returned. A
    later retry either commits again (no idempotency), replays the stored
    payment (server idempotency with a key), or never arrives.
    """

    if not 400 <= retryable_status <= 599:
        raise ValueError("retryable_status must be an HTTP error status")

    app = Flask(__name__)
    seen_keys: dict[str, dict[str, Any]] = {}
    failed_operations: set[str] = set()

    @app.post("/payments")
    def create_payment() -> tuple[Any, int, dict[str, str]]:
        body = request.get_json(silent=True) or {}
        attempt_id = request.headers.get("X-Attempt-Id")
        operation_id = request.headers.get("X-Operation-Id")
        idempotency_key = request.headers.get("Idempotency-Key")
        response_headers = {"X-Attempt-Id": attempt_id or ""}

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
            return (
                jsonify(seen_keys[idempotency_key]),
                201,
                response_headers,
            )

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

        operation_key = operation_id or ""
        if operation_key not in failed_operations:
            failed_operations.add(operation_key)
            log.record(
                EventKind.RETRYABLE_ERROR_RETURNED,
                attempt_id=attempt_id,
                operation_id=operation_id,
                status_code=retryable_status,
            )
            return (
                jsonify({"error": "retry later"}),
                retryable_status,
                response_headers,
            )

        return jsonify(payment), 201, response_headers

    return app