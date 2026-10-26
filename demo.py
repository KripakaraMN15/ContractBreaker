"""Milestone-1 demo: reproduce the duplicate, then test each partial fix."""

from examples.payments.scenario import PaymentScenarioConfig, run_payment_scenario

CASES = [
    (
        "buggy client + buggy server",
        PaymentScenarioConfig(
            server_supports_idempotency=False,
            client_sends_idempotency_key=False,
        ),
    ),
    (
        "server-only idempotency",
        PaymentScenarioConfig(
            server_supports_idempotency=True,
            client_sends_idempotency_key=False,
        ),
    ),
    (
        "idempotency key on both sides",
        PaymentScenarioConfig(
            server_supports_idempotency=True,
            client_sends_idempotency_key=True,
        ),
    ),
]


def main() -> None:
    for label, config in CASES:
        report = run_payment_scenario(config)
        kinds = " -> ".join(
            event.kind.value
            for event in report.events
            if event.kind.value
            in {
                "request_sent",
                "side_effect_committed",
                "response_dropped",
                "idempotent_replay",
            }
        )
        print(f"\n[{label}]")
        print(f"  verdict={report.verdict.value}  side_effects={report.side_effects}")
        print(f"  reason: {report.reason}")
        print(f"  evidence: {kinds}")


if __name__ == "__main__":
    main()