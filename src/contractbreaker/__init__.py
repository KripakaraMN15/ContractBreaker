"""ContractBreaker: deterministic causal verification of retry conflicts."""

from .events import Event, EventKind, EventLog
from .faults import FaultInjectionAdapter, FaultPlan
from .verifier import Report, Verdict, verify_duplicate_side_effects

__all__ = [
    "Event",
    "EventKind",
    "EventLog",
    "FaultInjectionAdapter",
    "FaultPlan",
    "Report",
    "Verdict",
    "verify_duplicate_side_effects",
]
