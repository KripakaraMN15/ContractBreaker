import requests

for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=0,
        )
    except requests.exceptions.Timeout:
        continue