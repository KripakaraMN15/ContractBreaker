"""Milestone-1 demo: duplicate payment reproduced, then fixed, same scenario."""
from contractbreaker import run_lost_response_scenario
from examples.payments.client import PaymentClient
from examples.payments.service import create_app


def run(label, server_idempotent, client_key):
    r = run_lost_response_scenario(
        make_app=lambda e, l: create_app(e, l, idempotent=server_idempotent),
        make_client=lambda s: PaymentClient(s, use_idempotency_key=client_key),
        call=lambda c: c.create_payment(1999),
    )
    print(f"\n[{label}] verdict={r.verdict.value}  side_effects={r.side_effects}")
    print(f"  reason: {r.reason}")
    print(f"  events: {' -> '.join(r.event_kinds)}")


run("buggy client + buggy server", False, False)
run("server-only fix           ", True, False)
run("idempotency key both sides", True, True)
