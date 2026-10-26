import requests

for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        json={"amount_cents": 1999},
        timeout=3,
    )
    if response.status_code >= 500:
        continue
    response.raise_for_status()
    break