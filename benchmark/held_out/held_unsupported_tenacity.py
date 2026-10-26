from tenacity import retry, stop_after_attempt
import requests


@retry(stop=stop_after_attempt(3))
def send_payment():
    return requests.post(
        "http://service.test/payments",
        json={"amount_cents": 1999},
        timeout=5,
    )


send_payment()