import requests

requests.post(
    "http://service.test/payments",
    json={"amount_cents": 4200},
    timeout=4,
)
requests.post(
    "http://service.test/payments",
    json={"amount_cents": 4200},
    timeout=4,
)
