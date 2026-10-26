import requests

configured_timeout = 5
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=configured_timeout,
        )
    except requests.exceptions.Timeout:
        continue