import requests

for attempt in range(1, 3):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 4200},
            timeout=4,
        )
    except requests.exceptions.Timeout:
        continue
