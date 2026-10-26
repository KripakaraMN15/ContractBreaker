import requests

for attempt in range(2):
    try:
        response = requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        continue