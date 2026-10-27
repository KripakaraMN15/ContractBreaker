import uuid
import requests

for attempt in range(2):
    idempotency_key = str(uuid.uuid4())
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
