import uuid
import requests

idempotency_key = str(uuid.uuid4())
response = requests.post(
    "http://service.test/payments",
    headers={"Idempotency-Key": idempotency_key},
    json={"amount_cents": 4200},
    timeout=4,
)
