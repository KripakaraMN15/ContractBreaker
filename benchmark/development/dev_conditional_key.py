import requests

use_key = True
fixed_key = "fixed-key"
headers = {}
for attempt in range(2):
    if use_key:
        headers["Idempotency-Key"] = fixed_key
    try:
        requests.post(
            "http://service.test/payments",
            headers=headers,
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue