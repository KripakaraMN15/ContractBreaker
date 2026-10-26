from contractbreaker import Verdict, run_lost_response_scenario
from examples.payments.client import PaymentClient
from examples.payments.service import create_app


def _run(server_idempotent: bool, client_key: bool, retries: int = 2):
    return run_lost_response_scenario(
        make_app=lambda effects, log: create_app(effects, log, idempotent=server_idempotent),
        make_client=lambda s: PaymentClient(s, use_idempotency_key=client_key, max_retries=retries),
        call=lambda c: c.create_payment(1999),
    )


def test_buggy_pair_is_confirmed_with_exact_causal_sequence():
    report = _run(server_idempotent=False, client_key=False)
    assert report.verdict is Verdict.CONFIRMED
    assert report.side_effects == 2
    assert report.event_kinds[:5] == [
        "request_sent", "side_effect_committed", "response_dropped",
        "request_sent", "side_effect_committed",
    ]


def test_fix_with_idempotency_key_is_not_reproduced():
    report = _run(server_idempotent=True, client_key=True)
    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1
    assert "idempotent_replay" in report.event_kinds


def test_client_that_never_retries_is_not_reproduced():
    report = _run(server_idempotent=False, client_key=False, retries=0)
    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1


def test_one_sided_fix_does_not_help():
    # Server supports keys but the client never sends one: still broken.
    report = _run(server_idempotent=True, client_key=False)
    assert report.verdict is Verdict.CONFIRMED
