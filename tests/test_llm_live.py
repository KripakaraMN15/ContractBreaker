"""Optional live OpenAI smoke test, skipped by default."""

from __future__ import annotations

import importlib.util
import os

import pytest

from contractbreaker.llm import LLMRequest, OpenAILLMClient

_LIVE_ENABLED = (
    os.environ.get("CONTRACTBREAKER_LIVE_LLM") == "1"
    and bool(os.environ.get("OPENAI_API_KEY"))
    and importlib.util.find_spec("openai") is not None
)


@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason=(
        "set CONTRACTBREAKER_LIVE_LLM=1, OPENAI_API_KEY, and install "
        "contractbreaker[llm] to run the paid live smoke test"
    ),
)
def test_live_openai_structured_extraction_smoke() -> None:
    client = OpenAILLMClient(timeout=30)
    response = client.extract(
        LLMRequest(
            filename="smoke.py",
            excerpt=(
                "   1: import requests\n"
                "   2: requests.get('https://example.com', timeout=5)"
            ),
            unresolved=(
                {
                    "target_id": "call:0",
                    "fact_type": "timeout",
                    "current_status": "unknown",
                    "current_value": None,
                    "reason": "smoke test",
                    "mergeable": True,
                },
            ),
            allowed_fact_types=("timeout", "method", "endpoint"),
            constraints=(
                "Use exact evidence from the excerpt.",
                "Do not infer values without evidence.",
            ),
        )
    )

    assert isinstance(response.proposals, tuple)
    assert response.model
    assert response.usage is None or response.usage.total_tokens >= 0