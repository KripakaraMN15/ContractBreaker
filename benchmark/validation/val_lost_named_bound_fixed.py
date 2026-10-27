import uuid
import requests

idempotency_key = str(uuid.uuid4())
attempts_allowed = 2
for attempt in range(attempts_allowed):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 4200},
            timeout=4,
        )
    except requests.exceptions.Timeout:
        continue
