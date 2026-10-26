import requests


def send_payment():
    for attempt in range(2):
        try:
            requests.post(
                "http://service.test/payments",
                json={"amount_cents": 1999},
                timeout=5,
            )
        except requests.exceptions.Timeout:
            continue


send_payment()