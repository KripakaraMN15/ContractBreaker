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
from .idempotency import IdempotencyKeyResult, check_idempotency_key_stability
from .retry_budget import (
    OUTCOME_BUDGET_EXCEEDED,
    OUTCOME_INSUFFICIENT_EVIDENCE,
    OUTCOME_WITHIN_BUDGET,
    RetryBudgetResult,
    check_retry_budget,
)
from .retryable_error_verifier import verify_retryable_error_duplicate
from .timeout_contract import (
    DEFAULT_TIMEOUT_INVARIANT,
    TIMEOUT_INVARIANTS,
    invariant_statement,
)
from .timeout_verifier import TimeoutResult, verify_client_timeout
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
    "DEFAULT_TIMEOUT_INVARIANT",
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
    "IdempotencyKeyResult",
    "NormalizedRequest",
    "IdempotencyKeyFact",
    "OUTCOME_BUDGET_EXCEEDED",
    "OUTCOME_INSUFFICIENT_EVIDENCE",
    "OUTCOME_WITHIN_BUDGET",
    "Report",
    "RetryBudgetResult",
    "RetryLoopFact",
    "ScenarioRecommendation",
    "StageOutcome",
    "StageResult",
    "SourceLocation",
    "TIMEOUT_INVARIANTS",
    "TimeoutResult",
    "Verdict",
    "WorkflowRequest",
    "WorkflowState",
    "__version__",
    "check_retry_budget",
    "check_idempotency_key_stability",
    "extract_from_file",
    "extract_from_source",
    "hypothesize_from_extraction",
    "hypothesize_from_file",
    "hypothesize_from_source",
    "interpret_request",
    "invariant_statement",
    "recommend_scenario",
    "render_workflow_text",
    "run_workflow",
    "verify_client_timeout",
    "verify_duplicate_side_effects",
    "verify_retryable_error_duplicate",
    "workflow_exit_code",
]

