import base64
import requests

idempotency_key = base64.b64encode(b"stable-key").decode("ascii")
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 4200},
            timeout=4,
        )
    except requests.exceptions.Timeout:
        continue
