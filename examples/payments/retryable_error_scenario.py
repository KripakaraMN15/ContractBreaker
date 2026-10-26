"""Distinct scenario: commit, retryable HTTP error, then client retry."""

from __future__ import annotations

from dataclasses import dataclass

import requests

from contractbreaker.events import EventKind, EventLog
from contractbreaker.faults import FaultInjectionAdapter, FaultPlan
from contractbreaker.retryable_error_verifier import (
    verify_retryable_error_duplicate,
)
from contractbreaker.verifier import Report

from .retryable_error_client import (
    BASE_URL,
    RetryableErrorClientError,
    RetryableErrorPaymentClient,
    RetryPolicy,
)
from .retryable_error_service import create_retryable_error_payment_app


@dataclass(frozen=True)
class RetryableErrorScenarioConfig:
    """Explicit policy and server behavior for the retryable-error scenario."""

    server_supports_idempotency: bool
    client_sends_idempotency_key: bool
    retry_statuses: frozenset[int] = frozenset({503})
    max_retries: int = 1
    request_timeout: float = 5.0
    server_error_status: int = 503

    def __post_init__(self) -> None:
        if not isinstance(self.retry_statuses, frozenset):
            object.__setattr__(
                self,
                "retry_statuses",
                frozenset(self.retry_statuses),
            )
        if any(
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
            for status in self.retry_statuses
        ):
            raise ValueError("retry_statuses must contain HTTP error statuses")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if not 400 <= self.server_error_status <= 599:
            raise ValueError("server_error_status must be an HTTP error status")


def run_retryable_error_scenario(
    config: RetryableErrorScenarioConfig,
) -> Report:
    """Run the retryable-error scenario and verify only its causal trace."""

    log = EventLog()
    ledger: list[dict[str, object]] = []
    app = create_retryable_error_payment_app(
        ledger,
        log,
        supports_idempotency=config.server_supports_idempotency,
        retryable_status=config.server_error_status,
    )

    session = requests.Session()
    session.mount(
        BASE_URL,
        # Reuse the in-process transport, but deliberately drop no responses.
        FaultInjectionAdapter(
            app,
            log,
            FaultPlan(drop_response_on_attempts=frozenset()),
        ),
    )
    client = RetryableErrorPaymentClient(
        session,
        log,
        policy=RetryPolicy(
            retry_statuses=config.retry_statuses,
            max_retries=config.max_retries,
        ),
        send_idempotency_key=config.client_sends_idempotency_key,
        timeout=config.request_timeout,
    )

    try:
        client.create_payment(1999)
        log.record(EventKind.SCENARIO_END, outcome="client_completed")
    except RetryableErrorClientError:
        log.record(EventKind.SCENARIO_END, outcome="client_gave_up")
    finally:
        session.close()

    return verify_retryable_error_duplicate(
        log.events, side_effects=len(ledger)
    )