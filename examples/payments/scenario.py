"""Wire the payment client, server, and fault adapter into one scenario."""

from __future__ import annotations

from dataclasses import dataclass

import requests

from contractbreaker.events import EventKind, EventLog
from contractbreaker.faults import FaultInjectionAdapter, FaultPlan
from contractbreaker.verifier import Report, verify_duplicate_side_effects

from .client import PaymentClient, PaymentClientError
from .service import create_payment_app

BASE_URL = "http://service.test"


@dataclass(frozen=True)
class PaymentScenarioConfig:
    """Knobs for the reference duplicate-payment scenario."""

    server_supports_idempotency: bool
    client_sends_idempotency_key: bool
    max_retries: int = 1
    drop_response_on_attempts: frozenset[int] = frozenset({1})

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not isinstance(self.drop_response_on_attempts, frozenset):
            object.__setattr__(
                self,
                "drop_response_on_attempts",
                frozenset(self.drop_response_on_attempts),
            )


def run_payment_scenario(config: PaymentScenarioConfig) -> Report:
    """Run one deterministic payment scenario and verify its event trace."""

    log = EventLog()
    ledger: list[dict[str, object]] = []
    app = create_payment_app(
        ledger,
        log,
        supports_idempotency=config.server_supports_idempotency,
    )

    session = requests.Session()
    session.mount(
        BASE_URL,
        FaultInjectionAdapter(
            app,
            log,
            FaultPlan(drop_response_on_attempts=config.drop_response_on_attempts),
        ),
    )
    client = PaymentClient(
        session,
        send_idempotency_key=config.client_sends_idempotency_key,
        max_retries=config.max_retries,
    )

    try:
        client.create_payment(1999)
        log.record(EventKind.SCENARIO_END, outcome="client_completed")
    except PaymentClientError:
        log.record(EventKind.SCENARIO_END, outcome="client_gave_up")
    finally:
        session.close()

    return verify_duplicate_side_effects(log.events, side_effects=len(ledger))