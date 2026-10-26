"""Tiny payment service. `idempotent=False` is the seeded bug."""

from flask import Flask, jsonify, request


def create_app(effects: list, log, idempotent: bool = False) -> Flask:
    app = Flask(__name__)
    seen_keys: dict[str, dict] = {}

    @app.post("/payments")
    def create_payment():
        body = request.get_json(silent=True) or {}
        key = request.headers.get("Idempotency-Key")

        if idempotent and key and key in seen_keys:
            log.record("idempotent_replay", key=key)
            return jsonify(seen_keys[key]), 201

        payment = {"id": len(effects) + 1, "amount_cents": body.get("amount_cents")}
        effects.append(payment)  # the side effect: money moves
        log.record("side_effect_committed", payment_id=payment["id"])

        if idempotent and key:
            seen_keys[key] = payment
        return jsonify(payment), 201

    return app
