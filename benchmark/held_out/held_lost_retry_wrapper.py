import requests


def submit_payment(amount_cents):
    """Retry helper deliberately kept out of module scope."""
    for attempt in range(2):
        try:
            return requests.post(
                "http://service.test/payments",
                json={"amount_cents": amount_cents},
                timeout=5,
            )
        except requests.exceptions.Timeout:
            continue
    return None


submit_payment(4200)
