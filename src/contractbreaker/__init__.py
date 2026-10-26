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
from .hypothesis import (
    ClientHypothesis,
    HypothesisNotRunnable,
    ScenarioRecommendation,
    hypothesize_from_extraction,
    hypothesize_from_file,
    hypothesize_from_source,
    recommend_scenario,
)
from .verifier import Report, Verdict, verify_duplicate_side_effects

__all__ = [
    "ClientHypothesis",
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
    "HypothesisNotRunnable",
    "IdempotencyKeyFact",
    "Report",
    "RetryLoopFact",
    "ScenarioRecommendation",
    "SourceLocation",
    "Verdict",
    "extract_from_file",
    "extract_from_source",
    "hypothesize_from_extraction",
    "hypothesize_from_file",
    "hypothesize_from_source",
    "recommend_scenario",
    "verify_duplicate_side_effects",
]

