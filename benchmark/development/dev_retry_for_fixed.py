import uuid
import requests

idempotency_key = str(uuid.uuid4())
for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        headers={"Idempotency-Key": idempotency_key},
        json={"amount_cents": 1999},
        timeout=5,
    )
    if response.status_code == 503:
        continue
    response.raise_for_status()
    break