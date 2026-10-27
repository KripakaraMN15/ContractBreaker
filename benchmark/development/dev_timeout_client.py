import requests

response = requests.post(
    "http://service.test/payments",
    json={"amount_cents": 1999},
    timeout=5,
)
