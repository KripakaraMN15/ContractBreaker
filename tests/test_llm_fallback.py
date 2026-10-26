"""Mocked tests for optional LLM extraction fallback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from contractbreaker.llm import (
    DEFAULT_LLM_MODEL,
    LLMInvalidOutput,
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    LLMUsage,
    OpenAILLMClient,
    estimate_cost_usd,
)
from contractbreaker.pipeline import AnalysisOptions, analyze

UNKNOWN_TIMEOUT_BUGGY = """import requests

configured_timeout = 5
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=configured_timeout,
        )
    except requests.exceptions.Timeout:
        continue
"""

UNKNOWN_TIMEOUT_NO_RETRY = """import requests

configured_timeout = 5
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=configured_timeout,
        )
    except requests.exceptions.ConnectionError:
        continue
"""

UNKNOWN_RETRY_KNOWN_TIMEOUT = """import requests

for attempt in range(max_retries + 1):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
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

NESTED_CLIENT = """import requests

def send():
    for attempt in range(2):
        try:
            requests.post("http://service.test/payments", timeout=5)
        except requests.exceptions.Timeout:
            continue
"""

RETRYABLE_STATUS_BUGGY = """import requests

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


def write_source(tmp_path: Path, source: str, name: str = "client.py") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def proposal(
    *,
    target_id: str,
    fact_type: str,
    value,
    start_line: int,
    excerpt: str,
    status: str = "known",
) -> dict:
    return {
        "target_id": target_id,
        "fact_type": fact_type,
        "value": value,
        "status": status,
        "evidence": [
            {
                "start_line": start_line,
                "end_line": start_line,
                "excerpt": excerpt,
            }
        ],
        "reason": "mock evidence-based proposal",
        "limitations": [],
    }


class FakeClient:
    model = "fake-test-model"

    def __init__(self, proposals=(), usage=None, error=None):
        self._proposals = tuple(proposals)
        self._usage = usage
        self._error = error
        self.requests: list[LLMRequest] = []

    def extract(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMResponse(
            proposals=self._proposals,
            model=self.model,
            usage=self._usage,
        )


def test_disabled_fallback_makes_no_api_call(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=5,
                start_line=3,
                excerpt="configured_timeout = 5",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            scenario="lost-response",
            llm_fallback=False,
            llm_client=fake,
        )
    )

    assert fake.requests == []
    assert report["hypothesis"]["llm_fallback"]["outcome"] == "disabled"
    assert report["hypothesis"]["timeout"]["status"] == "unknown"
    assert all(
        value == "derived_from_extraction"
        for value in report["hypothesis"]["fact_provenance"].values()
    )
    assert report["verdict"] == "inconclusive"


def test_valid_evidence_can_fill_unknown_timeout_and_verify_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTRACTBREAKER_LLM_INPUT_USD_PER_1M", "0.4")
    monkeypatch.setenv("CONTRACTBREAKER_LLM_OUTPUT_USD_PER_1M", "1.6")
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=5,
                start_line=3,
                excerpt="configured_timeout = 5",
            ),
        ),
        usage=LLMUsage(120, 40, 160),
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            scenario="lost-response",
            llm_fallback=True,
            llm_client=fake,
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["outcome"] == "success"
    assert llm["api_called"] is True
    assert llm["accepted_facts"][0]["fact_type"] == "timeout"
    assert llm["accepted_facts"][0]["applied"] is True
    assert report["hypothesis"]["timeout"]["status"] == "known"
    assert report["hypothesis"]["timeout"]["value"] == 5
    assert report["hypothesis"]["fact_provenance"]["timeout"] == "llm_assisted"
    assert llm["runnability_before"] is False
    assert llm["runnability_after"] is True
    assert llm["usage"] == {
        "input_tokens": 120,
        "output_tokens": 40,
        "total_tokens": 160,
    }
    assert llm["estimated_cost_usd"] == 0.000112
    assert report["execution"]["occurred"] is True
    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2


def test_missing_evidence_is_rejected(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    missing = proposal(
        target_id="call:0",
        fact_type="timeout",
        value=5,
        start_line=3,
        excerpt="configured_timeout = 5",
    )
    missing["evidence"] = []
    fake = FakeClient(proposals=(missing,))

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )

    assert report["hypothesis"]["llm_fallback"]["accepted_facts"] == []
    assert any(
        "evidence must be a non-empty list" in error
        for item in report["hypothesis"]["llm_fallback"]["rejected_facts"]
        for error in item.get("errors", [])
    )
    assert report["hypothesis"]["timeout"]["status"] == "unknown"


def test_mismatched_evidence_is_rejected(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=5,
                start_line=3,
                excerpt="configured_timeout = 99",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["outcome"] == "success"
    assert llm["accepted_facts"] == []
    assert llm["rejected_facts"]
    assert any(
        "does not match the source" in error
        for item in llm["rejected_facts"]
        for error in item.get("errors", [])
    )
    assert report["hypothesis"]["timeout"]["status"] == "unknown"
    assert report["verdict"] == "inconclusive"


def test_invalid_line_range_is_rejected(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=5,
                start_line=999,
                excerpt="configured_timeout = 5",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )

    assert report["hypothesis"]["llm_fallback"]["accepted_facts"] == []
    assert report["hypothesis"]["timeout"]["status"] == "unknown"


def test_hallucinated_retry_limit_is_rejected(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_RETRY_KNOWN_TIMEOUT)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="loop:0",
                fact_type="retry_count",
                value=1,
                start_line=3,
                excerpt="for attempt in range(max_retries + 1):",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["accepted_facts"] == []
    assert llm["rejected_facts"]
    assert report["hypothesis"]["retry_count"]["status"] == "unknown"
    assert report["hypothesis"]["scenario_readiness"]["runnable"] is False
    assert report["execution"]["occurred"] is False
    assert report["verdict"] == "inconclusive"


def test_deterministic_fact_conflict_is_preserved(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_RETRY_KNOWN_TIMEOUT)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=10,
                start_line=7,
                excerpt="            timeout=5,",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["conflicts"]
    assert llm["conflicts"][0]["kind"] == "deterministic_conflict"
    assert llm["accepted_facts"] == []
    assert report["hypothesis"]["timeout"]["status"] == "known"
    assert report["hypothesis"]["timeout"]["value"] == 5
    assert report["hypothesis"]["scenario_readiness"]["runnable"] is False


def test_missing_api_key_preserves_deterministic_result(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=None,
            scenario="lost-response",
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["outcome"] == "unavailable"
    assert llm["api_called"] is False
    assert "missing_api_key" in llm["error"]
    assert report["hypothesis"]["timeout"]["status"] == "unknown"
    assert report["verdict"] == "inconclusive"


def test_api_failure_preserves_confirmed_deterministic_result(tmp_path) -> None:
    source = write_source(tmp_path, RETRYABLE_STATUS_BUGGY)
    fake = FakeClient(
        error=LLMUnavailable("rate_limit", "simulated rate limit")
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            scenario="retryable-error",
            retry_statuses=(503,),
            server_error_status=503,
            llm_fallback=True,
            llm_client=fake,
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["outcome"] == "unavailable"
    assert llm["api_called"] is True
    assert report["execution"]["occurred"] is True
    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2


def test_ambiguity_blocks_llm_merge_and_readiness(tmp_path) -> None:
    source = write_source(tmp_path, CONDITIONAL_KEY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="idempotency_key_present",
                value=True,
                start_line=8,
                excerpt='        headers["Idempotency-Key"] = fixed_key',
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )
    llm = report["hypothesis"]["llm_fallback"]

    assert llm["outcome"] == "success"
    assert llm["accepted_facts"] == []
    assert any(
        conflict["kind"] == "non_mergeable_deterministic_status"
        for conflict in llm["conflicts"]
    )
    assert report["hypothesis"]["idempotency_key_present"]["status"] == "ambiguous"
    assert report["hypothesis"]["scenario_readiness"]["runnable"] is False
    assert report["execution"]["occurred"] is False


def test_nested_runtime_pattern_stays_non_runnable(tmp_path) -> None:
    source = write_source(tmp_path, NESTED_CLIENT)
    fake = FakeClient()

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )

    assert fake.requests == []
    assert report["analysis_status"] == "not_runnable"
    assert report["execution"]["occurred"] is False
    assert report["hypothesis"]["llm_fallback"]["outcome"] in {
        "not_needed",
        "disabled",
    }


def test_llm_filled_facts_do_not_force_confirmed_verdict(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_NO_RETRY)
    fake = FakeClient(
        proposals=(
            proposal(
                target_id="call:0",
                fact_type="timeout",
                value=5,
                start_line=3,
                excerpt="configured_timeout = 5",
            ),
        )
    )

    report = analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )

    assert report["hypothesis"]["timeout"]["status"] == "known"
    assert report["execution"]["occurred"] is True
    assert report["verdict"] != "confirmed"
    assert report["verdict"] == "not_reproduced"


def test_prompt_contains_no_benchmark_labels(tmp_path) -> None:
    source = write_source(tmp_path, UNKNOWN_TIMEOUT_BUGGY)
    fake = FakeClient()

    analyze(
        AnalysisOptions(
            source=source,
            llm_fallback=True,
            llm_client=fake,
            scenario="lost-response",
        )
    )

    assert len(fake.requests) == 1
    payload = json.dumps(
        {
            "filename": fake.requests[0].filename,
            "excerpt": fake.requests[0].excerpt,
            "unresolved": fake.requests[0].unresolved,
            "constraints": fake.requests[0].constraints,
        },
        sort_keys=True,
    )
    for leaked in (
        "confirmed",
        "not_reproduced",
        "expected",
        "benchmark",
        "dev-unknown-timeout",
    ):
        assert leaked not in payload


class _Completions:
    def __init__(self, result):
        self._result = result

    def create(self, **kwargs):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _Chat:
    def __init__(self, result):
        self.completions = _Completions(result)


class _FakeSdk:
    def __init__(self, result):
        self.chat = _Chat(result)


class APITimeoutError(Exception):
    pass


class RateLimitError(Exception):
    pass


def _response(content, refusal=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=refusal)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
        model="test-model",
        id="req-1",
    )


def _request() -> LLMRequest:
    return LLMRequest(
        filename="client.py",
        excerpt="   1: import requests",
        unresolved=(),
        allowed_fact_types=("timeout",),
        constraints=("evidence required",),
    )


def test_openai_client_valid_structured_response() -> None:
    client = OpenAILLMClient(
        model="test-model",
        sdk_client=_FakeSdk(
            _response('{"proposals": [{"target_id": "call:0"}]}')
        ),
    )

    response = client.extract(_request())

    assert response.proposals == ({"target_id": "call:0"},)
    assert response.usage == LLMUsage(10, 5, 15)
    assert response.model == "test-model"


def test_openai_client_maps_timeout_and_rate_limit() -> None:
    timeout_client = OpenAILLMClient(
        model="test-model", sdk_client=_FakeSdk(APITimeoutError("slow"))
    )
    with pytest.raises(LLMUnavailable) as timeout_error:
        timeout_client.extract(_request())
    assert timeout_error.value.code == "timeout"

    rate_client = OpenAILLMClient(
        model="test-model", sdk_client=_FakeSdk(RateLimitError("slow down"))
    )
    with pytest.raises(LLMUnavailable) as rate_error:
        rate_client.extract(_request())
    assert rate_error.value.code == "rate_limit"


def test_openai_client_handles_refusal_and_malformed_output() -> None:
    refusal_client = OpenAILLMClient(
        model="test-model",
        sdk_client=_FakeSdk(_response(None, refusal="cannot help")),
    )
    with pytest.raises(LLMInvalidOutput) as refusal_error:
        refusal_client.extract(_request())
    assert refusal_error.value.code == "refusal"

    malformed_client = OpenAILLMClient(
        model="test-model",
        sdk_client=_FakeSdk(_response("not json")),
    )
    with pytest.raises(LLMInvalidOutput) as malformed_error:
        malformed_client.extract(_request())
    assert malformed_error.value.code == "malformed_output"


def test_model_configuration_and_cost_estimate(monkeypatch) -> None:
    monkeypatch.delenv("CONTRACTBREAKER_LLM_MODEL", raising=False)
    default_client = OpenAILLMClient(
        sdk_client=_FakeSdk(_response('{"proposals": []}'))
    )
    assert default_client.model == DEFAULT_LLM_MODEL

    monkeypatch.setenv("CONTRACTBREAKER_LLM_MODEL", "env-model")
    env_client = OpenAILLMClient(
        sdk_client=_FakeSdk(_response('{"proposals": []}'))
    )
    assert env_client.model == "env-model"

    monkeypatch.delenv("CONTRACTBREAKER_LLM_INPUT_USD_PER_1M", raising=False)
    monkeypatch.delenv("CONTRACTBREAKER_LLM_OUTPUT_USD_PER_1M", raising=False)
    usage = LLMUsage(1_000_000, 500_000, 1_500_000)
    assert estimate_cost_usd(usage) is None

    monkeypatch.setenv("CONTRACTBREAKER_LLM_INPUT_USD_PER_1M", "0.4")
    monkeypatch.setenv("CONTRACTBREAKER_LLM_OUTPUT_USD_PER_1M", "1.6")
    assert estimate_cost_usd(usage) == 1.2


def test_openai_client_rejects_invalid_top_level_shape() -> None:
    client = OpenAILLMClient(
        model="test-model",
        sdk_client=_FakeSdk(_response('{"proposals": [], "verdict": "confirmed"}')),
    )

    with pytest.raises(LLMInvalidOutput) as error:
        client.extract(_request())

    assert error.value.code == "invalid_output"