import requests

for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        json={"amount_cents": 4200},
        timeout=4,
    )
    if response.status_code == 503:
        continue
    break
