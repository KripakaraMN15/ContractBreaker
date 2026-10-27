"""Declared reliability contracts for the client-timeout scenario.

A timeout being observed is a fact. Whether it is a *bug* depends on the
contract the operator declares. These statements are the single source of truth
used by the scenario configuration, the report, and the verifier.
"""

from __future__ import annotations

DEFAULT_TIMEOUT_INVARIANT = "abort-on-timeout"

TIMEOUT_INVARIANTS: dict[str, str] = {
    "abort-on-timeout": (
        "a side effect must not be committed for an operation after the client "
        "observed a timeout"
    ),
    "allow-orphaned-commit": (
        "the server may finish processing after the client observed a timeout, "
        "so an unacknowledged commit is permitted"
    ),
}


def invariant_statement(invariant_id: str) -> str:
    """Return the human-readable statement for a declared invariant."""

    return TIMEOUT_INVARIANTS[invariant_id]
