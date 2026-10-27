import requests

attempts_allowed = 2
for attempt in range(attempts_allowed):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 4200},
            timeout=4,
        )
    except requests.exceptions.Timeout:
        continue
