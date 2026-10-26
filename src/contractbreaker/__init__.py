"""ContractBreaker: deterministic causal verification of retry conflicts."""

from .ast_extractor import extract_from_file, extract_from_source
from .events import Event, EventKind, EventLog
from .extraction import (
    ExtractionResult,
    ExtractionStatus,
    ExtractionWarning,
    Fact,
    FactStatus,
    HeaderFact,
    HttpCallFact,
    IdempotencyKeyFact,
    RetryLoopFact,
    SourceLocation,
)
from .faults import FaultInjectionAdapter, FaultPlan
from .verifier import Report, Verdict, verify_duplicate_side_effects

__all__ = [
    "Event",
    "EventKind",
    "EventLog",
    "ExtractionResult",
    "ExtractionStatus",
    "ExtractionWarning",
    "Fact",
    "FactStatus",
    "FaultInjectionAdapter",
    "FaultPlan",
    "HeaderFact",
    "HttpCallFact",
    "IdempotencyKeyFact",
    "Report",
    "RetryLoopFact",
    "SourceLocation",
    "Verdict",
    "extract_from_file",
    "extract_from_source",
    "verify_duplicate_side_effects",
]
