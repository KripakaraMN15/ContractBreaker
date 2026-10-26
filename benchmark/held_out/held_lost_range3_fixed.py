import secrets
import requests

idempotency_key = secrets.token_hex(16)
for attempt in range(3):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 1999},
            timeout=2.5,
        )
    except requests.exceptions.Timeout:
        continue