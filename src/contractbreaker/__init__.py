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
from .retryable_error_verifier import verify_retryable_error_duplicate
from .verifier import Report, Verdict, verify_duplicate_side_effects
from .workflow import (
    ApprovalState,
    NormalizedRequest,
    StageOutcome,
    StageResult,
    WorkflowRequest,
    WorkflowState,
    interpret_request,
    render_workflow_text,
    run_workflow,
    workflow_exit_code,
)
from .version import __version__

__all__ = [
    "ApprovalState",
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
    "NormalizedRequest",
    "IdempotencyKeyFact",
    "Report",
    "RetryLoopFact",
    "ScenarioRecommendation",
    "StageOutcome",
    "StageResult",
    "SourceLocation",
    "Verdict",
    "WorkflowRequest",
    "WorkflowState",
    "__version__",
    "extract_from_file",
    "extract_from_source",
    "hypothesize_from_extraction",
    "hypothesize_from_file",
    "hypothesize_from_source",
    "interpret_request",
    "recommend_scenario",
    "render_workflow_text",
    "run_workflow",
    "verify_duplicate_side_effects",
    "verify_retryable_error_duplicate",
    "workflow_exit_code",
]

