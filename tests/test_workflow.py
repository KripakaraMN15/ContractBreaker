"""Workflow orchestration tests: planning, approval, evidence, and safety."""

from __future__ import annotations

import json
from pathlib import Path

from contractbreaker.cli import main
from contractbreaker.explain import (
    build_deterministic_explanation,
    summarize_report,
    validate_summary,
)
from contractbreaker.llm import (
    JSONCompletion,
    LLMResponse,
    LLMUnavailable,
    LLMUsage,
)
from contractbreaker.workflow import (
    ApprovalState,
    StageOutcome,
    WorkflowRequest,
    interpret_request,
    run_workflow,
    workflow_exit_code,
)
from contractbreaker.report import (
    EXIT_CONFIRMED,
    EXIT_INCONCLUSIVE,
    EXIT_INVALID_ARGS,
    EXIT_NOT_REPRODUCED,
    EXIT_UNSUPPORTED_INPUT,
)

BUGGY = """import requests

for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

FIXED = """import uuid
import requests

idempotency_key = str(uuid.uuid4())
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

UNKNOWN_RETRY = """import requests

for attempt in range(max_retries + 1):
    try:
        requests.post(
            "http://service.test/payments",
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""

UNSUPPORTED = """import httpx
httpx.post("http://service.test/payments", timeout=1)
"""

NESTED = """import requests

def send():
    for attempt in range(2):
        try:
            requests.post("http://service.test/payments", timeout=5)
        except requests.exceptions.Timeout:
            continue
"""

RETRYABLE = """import requests

for attempt in range(2):
    response = requests.post(
        "http://service.test/payments",
        json={"amount_cents": 1999},
        timeout=5,
    )
    if response.status_code == 503:
        continue
    response.raise_for_status()
    break
"""

CONDITIONAL_KEY = """import requests

use_key = True
fixed_key = "fixed-key"
headers = {}
for attempt in range(2):
    if use_key:
        headers["Idempotency-Key"] = fixed_key
    try:
        requests.post(
            "http://service.test/payments",
            headers=headers,
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""


def write_source(tmp_path: Path, source: str, name: str = "client.py") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_deterministic_scenario_selection(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)
    lost = interpret_request(
        WorkflowRequest(
            request="check duplicate charge after a lost response",
            source=source,
        )
    )
    retry = interpret_request(
        WorkflowRequest(
            request="does the client retry when the server returns 503",
            source=source,
        )
    )

    assert lost.stage.outcome is StageOutcome.SUCCESS
    assert lost.normalized is not None
    assert lost.normalized.scenario == "lost-response"
    assert lost.normalized.scenario_source == "request_text"

    assert retry.stage.outcome is StageOutcome.SUCCESS
    assert retry.normalized is not None
    assert retry.normalized.scenario == "retryable-error"
    assert retry.normalized.scenario_source == "request_text"


def test_structured_scenario_override_wins(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)
    interpretation = interpret_request(
        WorkflowRequest(
            request="investigate duplicate payments",
            source=source,
            scenario="retryable-error",
        )
    )

    assert interpretation.stage.outcome is StageOutcome.SUCCESS
    assert interpretation.normalized is not None
    assert interpretation.normalized.scenario == "retryable-error"
    assert interpretation.normalized.scenario_source == "user_structured"


def test_ambiguous_and_underspecified_requests_are_blocked() -> None:
    both = interpret_request(
        WorkflowRequest(
            request="retry on timeout and on a 503 server error",
        )
    )
    vague = interpret_request(
        WorkflowRequest(request="please check my payment client")
    )

    assert both.stage.outcome is StageOutcome.BLOCKED
    assert "both scenarios" in both.stage.summary
    assert both.stage.details["blocked_kind"] == "invalid_request"

    assert vague.stage.outcome is StageOutcome.BLOCKED
    assert vague.stage.details["blocked_kind"] == "invalid_request"
    assert vague.normalized is None


def test_unsupported_request_topic_is_blocked() -> None:
    interpretation = interpret_request(
        WorkflowRequest(request="check idempotency around a circuit breaker")
    )

    assert interpretation.stage.outcome is StageOutcome.BLOCKED
    assert interpretation.stage.details["blocked_kind"] == "unsupported_request"


def test_missing_source_is_underspecified(tmp_path) -> None:
    state = run_workflow(
        WorkflowRequest(request="check a lost response", approved=True)
    )

    assert state.outcome == "blocked"
    assert workflow_exit_code(state) == EXIT_INVALID_ARGS
    assert "source" in state.snapshot()["stages"][0]["summary"].lower()


def test_source_can_be_resolved_from_request_text(tmp_path) -> None:
    write_source(tmp_path, BUGGY)
    interpreted = interpret_request(
        WorkflowRequest(
            request=f"check lost response in {tmp_path / 'client.py'}",
        )
    )

    assert interpreted.stage.outcome is StageOutcome.SUCCESS
    assert interpreted.normalized is not None
    assert interpreted.normalized.source.name == "client.py"


def test_plan_only_never_executes(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
            plan_only=True,
        )
    )

    assert state.outcome == "planned"
    assert state.report is None
    assert state.verdict is None
    assert workflow_exit_code(state) == EXIT_INCONCLUSIVE
    assert [stage.stage for stage in state.stages] == [
        "request_interpretation",
        "source_analysis",
        "readiness_validation",
        "approval",
    ]
    assert state.stages[-1].details["blocked_kind"] == "plan_only"


def test_approval_is_required_before_execution(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)

    state = run_workflow(
        WorkflowRequest(request="check a lost response", source=source)
    )

    assert state.outcome == "planned"
    assert state.approval is ApprovalState.PENDING
    assert state.report is None
    assert workflow_exit_code(state) == EXIT_INCONCLUSIVE
    assert state.stages[-1].details["blocked_kind"] == "approval_required"


def test_approved_buggy_run_confirms_and_preserves_verifier_verdict(
    tmp_path,
) -> None:
    source = write_source(tmp_path, BUGGY)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "executed"
    assert state.approval is ApprovalState.APPROVED
    assert state.verdict == "confirmed"
    assert workflow_exit_code(state) == EXIT_CONFIRMED
    assert state.report is not None
    assert state.report["verdict_provenance"] == "runtime_verifier"
    assert state.report["execution"]["observed_side_effects"] == 2
    assert [stage.stage for stage in state.stages] == [
        "request_interpretation",
        "source_analysis",
        "readiness_validation",
        "approval",
        "execution",
        "explanation",
    ]


def test_approved_fixed_run_is_not_reproduced(tmp_path) -> None:
    source = write_source(tmp_path, FIXED)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "executed"
    assert state.verdict == "not_reproduced"
    assert workflow_exit_code(state) == EXIT_NOT_REPRODUCED
    assert state.report is not None
    assert state.report["execution"]["observed_side_effects"] == 1


def test_retryable_error_workflow_reuses_scenario_verifier(tmp_path) -> None:
    source = write_source(tmp_path, RETRYABLE)

    state = run_workflow(
        WorkflowRequest(
            request="check retry behavior on a 503 server error",
            source=source,
            approved=True,
        )
    )

    assert state.normalized is not None
    assert state.normalized.scenario == "retryable-error"
    assert state.verdict == "confirmed"
    assert state.report is not None
    assert state.report["scenario_configuration"]["runner"] == (
        "runtime_subprocess"
    )
    assert any(
        event["kind"] == "retry_scheduled"
        for event in state.report["execution"]["events"]
    )


def test_blocked_client_never_reaches_execution(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_RETRY)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "blocked"
    assert state.report is None
    assert [stage.stage for stage in state.stages] == [
        "request_interpretation",
        "source_analysis",
        "readiness_validation",
    ]
    readiness = state.stages[-1]
    assert readiness.outcome is StageOutcome.BLOCKED
    assert any("retry count" in blocker for blocker in readiness.blockers)
    assert workflow_exit_code(state) == EXIT_INCONCLUSIVE


def test_unsupported_source_blocks_with_structured_explanation(tmp_path) -> None:
    source = write_source(tmp_path, UNSUPPORTED)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "blocked"
    assert state.report is None
    assert workflow_exit_code(state) == EXIT_UNSUPPORTED_INPUT
    analysis_stage = state.stages[1]
    assert analysis_stage.outcome is StageOutcome.BLOCKED
    assert any(
        "unsupported" in blocker for blocker in analysis_stage.blockers
    )


def test_runtime_contract_blocks_nested_client(tmp_path) -> None:
    source = write_source(tmp_path, NESTED)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "blocked"
    assert state.report is None
    assert any(
        "module-level" in blocker
        for stage in state.stages
        for blocker in stage.blockers
    )


def test_conflicting_llm_facts_are_visible_and_do_not_force_execution(
    tmp_path,
) -> None:
    source = write_source(tmp_path, CONDITIONAL_KEY)

    class ConflictClient:
        model = "fake-conflict-model"

        def extract(self, request):
            return LLMResponse(
                proposals=(
                    {
                        "target_id": "call:0",
                        "fact_type": "idempotency_key_present",
                        "value": True,
                        "status": "known",
                        "evidence": [
                            {
                                "start_line": 8,
                                "end_line": 8,
                                "excerpt": (
                                    '        headers["Idempotency-Key"] = fixed_key'
                                ),
                            }
                        ],
                        "reason": "conditional header assigned",
                        "limitations": ["conditional"],
                    },
                ),
                model=self.model,
                usage=LLMUsage(10, 5, 15),
            )

        def complete_json(self, **kwargs):
            raise LLMUnavailable("unused", "not used in this test")

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
            llm_fallback=True,
            llm_client=ConflictClient(),
        )
    )

    assert state.outcome == "blocked"
    assert state.report is None
    analysis_stage = state.stages[1]
    assert analysis_stage.outcome is StageOutcome.SUCCESS
    assert analysis_stage.details["llm_fallback"]["conflicts"] == 1
    readiness = state.stages[2]
    assert readiness.outcome is StageOutcome.BLOCKED
    assert any(
        "idempotency key presence is unresolved" in blocker
        for blocker in readiness.blockers
    )


class SummaryClient:
    model = "fake-summary-model"

    def __init__(self, data: dict, *, error=None):
        self.data = data
        self.error = error
        self.calls = 0

    def extract(self, request):
        raise LLMUnavailable("unused", "extraction not requested")

    def complete_json(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return JSONCompletion(
            data=self.data,
            model=self.model,
            usage=LLMUsage(50, 20, 70),
        )


def _buggy_report(tmp_path) -> dict:
    source = write_source(tmp_path, BUGGY)
    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )
    assert state.report is not None
    return state.report


def test_summarizer_cannot_change_verdict_or_side_effect_count(tmp_path) -> None:
    report = _buggy_report(tmp_path)

    liar = SummaryClient(
        {
            "summary": "No duplicate occurred; 1 side effects.",
            "observed": "1 side effect.",
            "expected": "1 side effect.",
            "duplicate_side_effects": False,
            "verdict": "not_reproduced",
        }
    )
    result = summarize_report(report, enabled=True, client=liar)

    assert result.outcome == "invalid_output"
    assert result.rejection_reason is not None
    assert "verdict" in result.rejection_reason
    assert "VERDICT: CONFIRMED" not in result.text
    assert "Verdict: CONFIRMED" in result.text
    assert "observed side effects=2" in result.text


def test_summarizer_rejects_invented_side_effect_counts(tmp_path) -> None:
    report = _buggy_report(tmp_path)
    completion = JSONCompletion(
        data={
            "summary": "The retry produced 3 side effects.",
            "observed": "Two attempts; 3 side effects.",
            "expected": "One side effect.",
            "duplicate_side_effects": True,
            "verdict": "confirmed",
        },
        model="fake-summary-model",
        usage=LLMUsage(10, 5, 15),
    )

    error = validate_summary(report, completion)

    assert error is not None
    assert "side-effect count" in error


def test_valid_llm_summary_is_accepted_and_verdict_preserved(tmp_path) -> None:
    report = _buggy_report(tmp_path)
    client = SummaryClient(
        {
            "summary": (
                "A retry committed a duplicate payment after the "
                "response was lost."
            ),
            "observed": "Two attempts and 2 side effects.",
            "expected": "One logical operation should commit once.",
            "duplicate_side_effects": True,
            "verdict": "confirmed",
        }
    )

    result = summarize_report(report, enabled=True, client=client)

    assert result.outcome == "success"
    assert client.calls == 1
    assert "Verdict: CONFIRMED" in result.text
    assert result.to_dict()["used_llm"] is True


def test_llm_summary_failure_falls_back_to_deterministic(tmp_path) -> None:
    report = _buggy_report(tmp_path)
    client = SummaryClient({}, error=LLMUnavailable("rate_limit", "slow down"))

    result = summarize_report(report, enabled=True, client=client)

    assert result.outcome == "unavailable"
    assert "Verdict: CONFIRMED" in result.text
    assert result.text == build_deterministic_explanation(report)


def test_llm_disabled_workflow_is_deterministic_and_never_calls_llm(
    tmp_path, monkeypatch
) -> None:
    source = write_source(tmp_path, BUGGY)

    def forbidden(**kwargs):
        raise AssertionError("LLM factory must not be called when disabled")

    monkeypatch.setattr(
        "contractbreaker.llm_fallback.create_default_llm_client", forbidden
    )
    monkeypatch.setattr(
        "contractbreaker.llm.create_default_llm_client", forbidden
    )

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.verdict == "confirmed"
    assert state.explanation is not None
    assert state.explanation.outcome == "disabled"


def test_llm_interpretation_requires_verbatim_evidence(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)

    class InterpretClient:
        model = "fake-interpret-model"

        def __init__(self, phrase: str):
            self.phrase = phrase

        def extract(self, request):
            raise LLMUnavailable("unused", "extraction not requested")

        def complete_json(self, **kwargs):
            return JSONCompletion(
                data={
                    "scenario": "lost-response",
                    "matched_phrase": self.phrase,
                    "reason": "the request describes an unanswered call",
                },
                model=self.model,
                usage=LLMUsage(10, 5, 15),
            )

    good = interpret_request(
        WorkflowRequest(
            request="my payment call takes too long and I try again",
            source=source,
            llm_fallback=True,
            llm_client=InterpretClient("takes too long"),
        )
    )
    invented = interpret_request(
        WorkflowRequest(
            request="my payment call takes too long and I try again",
            source=source,
            llm_fallback=True,
            llm_client=InterpretClient("server returned 503"),
        )
    )

    assert good.stage.outcome is StageOutcome.SUCCESS
    assert good.normalized is not None
    assert good.normalized.scenario_source == "llm_proposal"
    assert invented.stage.outcome is StageOutcome.BLOCKED


def test_workflow_cli_plan_does_not_execute(tmp_path, capsys) -> None:
    source = write_source(tmp_path, BUGGY)

    code = main(
        [
            "workflow",
            "--request",
            "check a lost response",
            "--source",
            str(source),
            "--plan-only",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert code == EXIT_INCONCLUSIVE
    assert document["outcome"] == "planned"
    assert document["analysis"] is None
    assert document["execution" if "execution" in document else "stages"]
    assert document["verdict"] is None


def test_workflow_cli_requires_approval_then_executes(tmp_path, capsys) -> None:
    source = write_source(tmp_path, BUGGY)
    output = tmp_path / "workflow.json"

    code = main(
        [
            "workflow",
            "--request",
            "check a lost response",
            "--source",
            str(source),
            "--format",
            "json",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    no_approval = json.loads(captured.out)

    assert code == EXIT_INCONCLUSIVE
    assert no_approval["analysis"] is None
    assert json.loads(output.read_text(encoding="utf-8"))["outcome"] == "planned"

    code = main(
        [
            "workflow",
            "--request",
            "check a lost response",
            "--source",
            str(source),
            "--approve",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    executed = json.loads(captured.out)

    assert code == EXIT_CONFIRMED
    assert executed["outcome"] == "executed"
    assert executed["analysis"]["verdict"] == "confirmed"
    assert executed["analysis"]["schema_version"] == "1.0"
    assert executed["verdict"] == "confirmed"
    assert executed["analysis_snapshot"]["hypothesis"]["fact_provenance"]


def test_workflow_cli_text_rendering(tmp_path, capsys) -> None:
    source = write_source(tmp_path, FIXED)

    code = main(
        [
            "workflow",
            "--request",
            "check a lost response",
            "--source",
            str(source),
            "--approve",
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_NOT_REPRODUCED
    assert "ContractBreaker workflow" in captured.out
    assert "[success] request_interpretation" in captured.out
    assert "Verdict: NOT_REPRODUCED" in captured.out
    assert "Observed:" in captured.out


def test_workflow_exit_codes_cover_invalid_and_unsupported(tmp_path, capsys) -> None:
    source = write_source(tmp_path, BUGGY)

    invalid = main(["workflow", "--request", "check my client"])
    assert invalid == EXIT_INVALID_ARGS

    unsupported = main(
        [
            "workflow",
            "--request",
            "check a circuit breaker",
            "--source",
            str(source),
        ]
    )
    assert unsupported == EXIT_UNSUPPORTED_INPUT

    missing = main(
        [
            "workflow",
            "--request",
            "check a lost response",
            "--source",
            str(tmp_path / "missing.py"),
        ]
    )
    assert missing == EXIT_UNSUPPORTED_INPUT


def test_workflow_snapshot_is_json_native_and_has_no_source_text(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)
    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )
    document = state.snapshot()
    serialized = json.dumps(document, sort_keys=True)

    assert set(document) == {
        "workflow_schema_version",
        "tool_version",
        "outcome",
        "request",
        "approval",
        "stages",
        "last_stage",
        "analysis_snapshot",
        "analysis",
        "verdict",
        "verdict_reason",
        "explanation",
        "explanation_meta",
        "limitations",
        "errors",
    }
    assert "requests.post(" not in serialized
    assert document["analysis"]["verdict"] == "confirmed"

def test_structured_input_without_free_text(tmp_path, capsys) -> None:
    source = write_source(tmp_path, BUGGY)

    code = main(
        [
            "workflow",
            "--scenario",
            "lost-response",
            "--source",
            str(source),
            "--plan-only",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert code == EXIT_INCONCLUSIVE
    assert document["request"]["scenario_source"] == "user_structured"
    assert document["stages"][0]["outcome"] == "success"
    assert any(
        "Structured input" in note
        for note in document["stages"][0]["details"]["notes"]
    )


def test_structured_input_requires_scenario_and_source(tmp_path, capsys) -> None:
    source = write_source(tmp_path, BUGGY)

    missing_scenario = main(["workflow", "--source", str(source)])
    assert missing_scenario == EXIT_INVALID_ARGS

    missing_source = main(
        ["workflow", "--scenario", "lost-response"]
    )
    assert missing_source == EXIT_INVALID_ARGS


def test_execution_failure_is_reported_without_verdict(tmp_path, monkeypatch) -> None:
    from contractbreaker.runtime import RuntimeExecutionError

    source = write_source(tmp_path, BUGGY)

    def fail(**kwargs):
        raise RuntimeExecutionError("simulated worker failure")

    monkeypatch.setattr("contractbreaker.pipeline.run_analyzed_client", fail)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "failed"
    assert workflow_exit_code(state) == EXIT_INCONCLUSIVE
    assert state.report is not None
    assert state.report["analysis_status"] == "not_runnable"
    assert state.report["execution"]["occurred"] is False
    assert state.report["execution"]["events"] == []
    execution_stage = next(
        stage for stage in state.stages if stage.stage == "execution"
    )
    assert execution_stage.outcome is StageOutcome.FAILED
    assert any(
        "simulated worker failure" in blocker
        for blocker in state.report["hypothesis"]["scenario_readiness"]["blockers"]
    )


def test_execution_stage_preserves_verifier_verdict(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )
    document = state.snapshot()
    execution_stage = next(
        stage for stage in document["stages"] if stage["stage"] == "execution"
    )

    assert execution_stage["details"]["verdict"] == state.report["verdict"]
    assert state.report["verdict_provenance"] == "runtime_verifier"
    assert document["explanation_meta"]["used_llm"] is False
    assert "Verdict: CONFIRMED" in document["explanation"]
    assert "observed side effects=2" in document["explanation"]


def test_incomplete_runtime_trace_is_inconclusive_in_workflow(tmp_path) -> None:
    source = write_source(
        tmp_path,
        """import requests

for attempt in range(2):
    try:
        raise RuntimeError("stop before request")
        requests.post("http://service.test/payments", timeout=5)
    except requests.exceptions.Timeout:
        continue
""",
        name="incomplete.py",
    )

    state = run_workflow(
        WorkflowRequest(
            request="check a lost response",
            source=source,
            approved=True,
        )
    )

    assert state.outcome == "executed"
    assert state.verdict == "inconclusive"
    assert workflow_exit_code(state) == EXIT_INCONCLUSIVE
    assert state.report is not None
    assert state.report["execution"]["occurred"] is True
    assert state.report["execution"]["observed_side_effects"] == 0

def test_numeric_status_markers_require_word_boundaries(tmp_path) -> None:
    source = write_source(tmp_path, BUGGY)

    interpretation = interpret_request(
        WorkflowRequest(
            request="check a lost response with a 1500ms timeout",
            source=source,
        )
    )

    assert interpretation.stage.outcome is StageOutcome.SUCCESS
    assert interpretation.normalized is not None
    assert interpretation.normalized.scenario == "lost-response"
