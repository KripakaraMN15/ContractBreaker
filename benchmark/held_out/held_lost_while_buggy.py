import requests

attempts = 0
while attempts < 2:
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
        break
    except requests.exceptions.Timeout:
        attempts += 1