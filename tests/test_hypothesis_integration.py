"""End-to-end extraction -> hypothesis -> reference scenario tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from contractbreaker import (
    ExtractionStatus,
    FactStatus,
    HypothesisNotRunnable,
    Verdict,
    extract_from_file,
    extract_from_source,
    hypothesize_from_extraction,
    hypothesize_from_source,
    recommend_scenario,
)
from examples.payments.scenario import (
    config_from_hypothesis,
    run_payment_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_CLIENT = ROOT / "examples" / "payments" / "client.py"

BUGGY_SOURCE = """import requests

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

PROTECTED_SOURCE = """import uuid
import requests

idempotency_key = str(uuid.uuid4())
for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""


def test_buggy_source_end_to_end_confirms_duplicate() -> None:
    result = extract_from_source(BUGGY_SOURCE, filename="buggy_client.py")
    hypothesis = hypothesize_from_extraction(result)

    assert result.status is ExtractionStatus.SUPPORTED
    assert hypothesis.scenario_ready is True
    assert hypothesis.runtime_verification_required is True
    assert hypothesis.retry_count.status is FactStatus.KNOWN
    assert hypothesis.retry_count.value == 1
    assert hypothesis.idempotency_key_present.status is FactStatus.ABSENT

    recommendation = recommend_scenario(hypothesis)
    assert recommendation.max_retries == 1
    assert recommendation.client_sends_idempotency_key is False
    assert recommendation.request_timeout == 5.0

    config = config_from_hypothesis(
        hypothesis, server_supports_idempotency=False
    )
    report = run_payment_scenario(config)

    assert report.verdict is Verdict.CONFIRMED
    assert report.side_effects == 2


def test_protected_source_end_to_end_is_not_reproduced() -> None:
    result = extract_from_source(PROTECTED_SOURCE, filename="protected_client.py")
    hypothesis = hypothesize_from_extraction(result)

    assert result.status is ExtractionStatus.SUPPORTED
    assert hypothesis.scenario_ready is True
    assert hypothesis.idempotency_key_present.status is FactStatus.KNOWN
    assert hypothesis.idempotency_key_reused.status is FactStatus.KNOWN
    assert hypothesis.idempotency_key_reused.value is True

    recommendation = recommend_scenario(hypothesis)
    assert recommendation.max_retries == 1
    assert recommendation.client_sends_idempotency_key is True

    config = config_from_hypothesis(
        hypothesis, server_supports_idempotency=True
    )
    report = run_payment_scenario(config)

    assert report.verdict is Verdict.NOT_REPRODUCED
    assert report.side_effects == 1


def test_reference_client_is_recognized_but_not_runnable() -> None:
    result = extract_from_file(REFERENCE_CLIENT)
    hypothesis = hypothesize_from_extraction(result)

    assert result.status is ExtractionStatus.PARTIAL
    assert len(result.calls) == 1
    assert result.calls[0].method.value == "POST"
    assert result.calls[0].timeout.status is FactStatus.UNKNOWN
    assert result.retry_loops[0].max_attempts.status is FactStatus.UNKNOWN
    assert result.calls[0].idempotency_key.status is FactStatus.AMBIGUOUS
    assert "unsupported.http_call" not in {
        warning.code for warning in result.warnings
    }

    assert hypothesis.scenario_ready is False
    assert hypothesis.runtime_verification_required is True
    assert any("timeout" in blocker for blocker in hypothesis.blockers)
    assert any("retry count" in blocker for blocker in hypothesis.blockers)
    assert any("idempotency" in blocker for blocker in hypothesis.blockers)
    assert not hasattr(hypothesis, "verdict")

    with pytest.raises(HypothesisNotRunnable) as error:
        recommend_scenario(hypothesis)
    assert error.value.blockers == hypothesis.blockers


def test_incomplete_retry_bound_cannot_become_runnable() -> None:
    source = """import requests
for attempt in range(max_retries + 1):
    try:
        requests.post(
            "http://service.test/payments",
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""
    result = extract_from_source(source)
    hypothesis = hypothesize_from_extraction(result)

    assert result.status is ExtractionStatus.PARTIAL
    assert hypothesis.retry_count.status is FactStatus.UNKNOWN
    assert hypothesis.scenario_ready is False
    assert any("retry count" in blocker for blocker in hypothesis.blockers)

    with pytest.raises(HypothesisNotRunnable):
        recommend_scenario(hypothesis)


def test_unsupported_extraction_cannot_become_runnable() -> None:
    source = """import httpx
httpx.post("http://service.test/payments", timeout=1)
"""
    result = extract_from_source(source)
    hypothesis = hypothesize_from_source(source)

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert hypothesis.http_call_count == 0
    assert hypothesis.scenario_ready is False
    assert any(
        "unsupported.http_client" in blocker for blocker in hypothesis.blockers
    )

    with pytest.raises(HypothesisNotRunnable):
        recommend_scenario(hypothesis)


def test_key_generated_inside_loop_is_scenario_ready() -> None:
    """A key that changes per attempt is testable, not a readiness blocker.

    Milestone 10 inverted this expectation: refusing to run the one client shape
    the experiment exists to evaluate made key instability unobservable.
    """

    source = """import uuid
import requests

for attempt in range(2):
    key = str(uuid.uuid4())
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": key},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""
    hypothesis = hypothesize_from_source(source)

    assert hypothesis.idempotency_key_present.status is FactStatus.KNOWN
    assert hypothesis.idempotency_key_reused.status is FactStatus.KNOWN
    assert hypothesis.idempotency_key_reused.value is False
    assert hypothesis.scenario_ready is True
    assert not any("reuse" in blocker for blocker in hypothesis.blockers)

    recommendation = recommend_scenario(hypothesis)
    assert recommendation.client_sends_idempotency_key is True
    assert recommendation.max_retries == 1


def test_unresolved_key_reuse_still_blocks_execution() -> None:
    source = """import requests
for attempt in range(2):
    key = make_key()
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": key},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
"""
    hypothesis = hypothesize_from_source(source)

    assert hypothesis.idempotency_key_present.status is FactStatus.KNOWN
    assert hypothesis.idempotency_key_reused.status is FactStatus.UNKNOWN
    assert hypothesis.scenario_ready is False
    assert any("reuse" in blocker for blocker in hypothesis.blockers)

    with pytest.raises(HypothesisNotRunnable):
        recommend_scenario(hypothesis)
