"""Labeled extraction cases for the narrow deterministic AST baseline."""

from __future__ import annotations

import json

import pytest

from contractbreaker import (
    ExtractionResult,
    ExtractionStatus,
    FactStatus,
    extract_from_file,
    extract_from_source,
)


def extract(source: str, *, filename: str = "example.py") -> ExtractionResult:
    return extract_from_source(source, filename=filename)


def test_supported_simple_get_with_literal_timeout() -> None:
    result = extract(
        """import requests
requests.get("https://api.test/items", timeout=5)
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.location.line == 2
    assert call.location.column == 1
    assert call.method.status is FactStatus.KNOWN
    assert call.method.value == "GET"
    assert call.endpoint.status is FactStatus.KNOWN
    assert call.endpoint.value == "https://api.test/items"
    assert call.timeout.status is FactStatus.KNOWN
    assert call.timeout.value == 5
    assert call.idempotency_key.status is FactStatus.ABSENT
    assert result.retry_loops == ()


def test_supported_multiline_post_headers_and_float_timeout() -> None:
    result = extract(
        """import requests
requests.post(
    url="https://api.test/payments",
    headers={
        "Content-Type": "application/json",
        "Idempotency-Key": "fixed-key",
    },
    timeout=2.5,
)
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    call = result.calls[0]
    assert call.location.line == 2
    assert call.location.end_line == 9
    assert call.method.value == "POST"
    assert call.endpoint.value == "https://api.test/payments"
    assert call.timeout.value == 2.5
    assert [header.name.value for header in call.headers] == [
        "Content-Type",
        "Idempotency-Key",
    ]
    assert call.headers[1].location.line == 6
    assert call.idempotency_key.status is FactStatus.KNOWN
    info = call.idempotency_key.value
    assert info is not None
    assert info.key_expression == "'fixed-key'"
    assert info.generated_inside_retry.status is FactStatus.ABSENT


def test_supported_module_alias_and_request_method_argument() -> None:
    result = extract(
        """import requests as http
http.request("PATCH", "https://api.test/items/1", timeout=1)
"""
    )

    call = result.calls[0]
    assert result.status is ExtractionStatus.SUPPORTED
    assert call.method.status is FactStatus.KNOWN
    assert call.method.value == "PATCH"
    assert call.endpoint.value == "https://api.test/items/1"


def test_supported_injected_annotated_session_method() -> None:
    result = extract(
        """import requests
class Client:
    def __init__(self, session: requests.Session):
        self._session = session
    def send(self):
        self._session.post("https://api.test/payments", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    assert len(result.calls) == 1
    assert result.calls[0].method.value == "POST"
    assert result.calls[0].endpoint.value == "https://api.test/payments"


def test_unannotated_injected_session_stays_unsupported() -> None:
    result = extract(
        """import requests
class Client:
    def __init__(self, session):
        self._session = session
    def send(self):
        self._session.post("https://api.test/payments", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert result.calls == ()
    assert "unsupported.http_call" in {warning.code for warning in result.warnings}


def test_conditional_idempotency_key_is_ambiguous() -> None:
    result = extract(
        """import requests
def send(use_key, key):
    headers = {}
    if use_key:
        headers["Idempotency-Key"] = key
    return requests.post(
        "https://api.test/payments", headers=headers, timeout=1
    )
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.calls[0].idempotency_key.status is FactStatus.AMBIGUOUS


def test_supported_tracked_session_method() -> None:
    result = extract(
        """import requests
session = requests.Session()
session.get("https://api.test/items", timeout=3)
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    assert result.calls[0].method.value == "GET"
    assert result.calls[0].timeout.value == 3


def test_supported_retry_loop_distinguishes_attempts_and_retries() -> None:
    result = extract(
        """import requests
for attempt in range(3):
    try:
        requests.post("https://api.test/pay", timeout=1.5)
    except requests.exceptions.Timeout:
        continue
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    assert len(result.retry_loops) == 1
    loop = result.retry_loops[0]
    assert loop.location.line == 2
    assert loop.max_attempts.status is FactStatus.KNOWN
    assert loop.max_attempts.value == 3
    assert loop.retry_count.status is FactStatus.KNOWN
    assert loop.retry_count.value == 2
    assert loop.triggers.status is FactStatus.KNOWN
    assert loop.triggers.value == ("requests.exceptions.Timeout",)
    assert result.calls[0].retry_loop is not None
    assert result.calls[0].retry_loop.line == 2


def test_supported_retry_loop_with_start_and_stop_bounds() -> None:
    result = extract(
        """import requests
for attempt in range(1, 5):
    try:
        requests.get("https://api.test/items", timeout=1)
    except requests.exceptions.ConnectionError:
        continue
"""
    )

    loop = result.retry_loops[0]
    assert result.status is ExtractionStatus.SUPPORTED
    assert loop.max_attempts.value == 4
    assert loop.retry_count.value == 3
    assert loop.triggers.value == ("requests.exceptions.ConnectionError",)


def test_supported_named_retry_limit_and_multiple_triggers() -> None:
    result = extract(
        """import requests
max_retries = 2
for attempt in range(max_retries + 1):
    try:
        requests.get("https://api.test/items", timeout=1)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        pass
"""
    )

    loop = result.retry_loops[0]
    assert result.status is ExtractionStatus.SUPPORTED
    assert loop.max_attempts.value == 3
    assert loop.retry_count.value == 2
    assert loop.triggers.value == (
        "requests.exceptions.Timeout",
        "requests.exceptions.ConnectionError",
    )


def test_supported_exception_alias_is_canonicalized() -> None:
    result = extract(
        """from requests.exceptions import Timeout as RequestTimeout
import requests
for attempt in range(2):
    try:
        requests.get("https://api.test/items", timeout=1)
    except RequestTimeout:
        continue
"""
    )

    loop = result.retry_loops[0]
    assert result.status is ExtractionStatus.SUPPORTED
    assert loop.triggers.value == ("requests.exceptions.Timeout",)


def test_idempotency_key_generated_outside_retry_loop_is_reused() -> None:
    result = extract(
        """import uuid
import requests
key = str(uuid.uuid4())
for attempt in range(2):
    try:
        requests.post(
            "https://api.test/pay",
            headers={"Idempotency-Key": key},
            timeout=1,
        )
    except requests.exceptions.Timeout:
        continue
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    info = result.calls[0].idempotency_key.value
    assert info is not None
    assert info.generated_inside_retry.status is FactStatus.KNOWN
    assert info.generated_inside_retry.value is False
    assert info.reused_across_attempts.status is FactStatus.KNOWN
    assert info.reused_across_attempts.value is True


def test_idempotency_key_generated_inside_retry_loop_is_not_reused() -> None:
    result = extract(
        """import uuid
import requests
for attempt in range(2):
    key = str(uuid.uuid4())
    try:
        requests.post(
            "https://api.test/pay",
            headers={"Idempotency-Key": key},
            timeout=1,
        )
    except requests.exceptions.Timeout:
        continue
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    info = result.calls[0].idempotency_key.value
    assert info is not None
    assert info.generated_inside_retry.value is True
    assert info.reused_across_attempts.status is FactStatus.KNOWN
    assert info.reused_across_attempts.value is False


def test_supported_header_variable_with_literal_key_and_update() -> None:
    result = extract(
        """import requests
headers = {"Idempotency-Key": "abc"}
headers["X-Trace"] = "1"
requests.post("https://api.test/pay", headers=headers, timeout=2)
"""
    )

    assert result.status is ExtractionStatus.SUPPORTED
    call = result.calls[0]
    assert [header.name.value for header in call.headers] == [
        "Idempotency-Key",
        "X-Trace",
    ]
    assert call.idempotency_key.status is FactStatus.KNOWN


@pytest.mark.parametrize(
    ("source", "expected_status", "expected_value", "reason_part"),
    [
        (
            """import requests
requests.get("https://api.test/items")
""",
            FactStatus.ABSENT,
            None,
            "no timeout argument",
        ),
        (
            """import requests
configured_timeout = 5
requests.get("https://api.test/items", timeout=configured_timeout)
""",
            FactStatus.UNKNOWN,
            None,
            "not a literal",
        ),
        (
            """import requests
requests.get("https://api.test/items", timeout=None)
""",
            FactStatus.KNOWN,
            None,
            "explicitly None",
        ),
    ],
    ids=["missing", "dynamic", "explicit-none"],
)
def test_timeout_absence_unknown_and_explicit_none_are_distinct(
    source: str,
    expected_status: FactStatus,
    expected_value: object,
    reason_part: str,
) -> None:
    call = extract(source).calls[0]

    assert call.timeout.status is expected_status
    assert call.timeout.value == expected_value
    if call.timeout.reason is not None:
        assert reason_part in call.timeout.reason


def test_dynamic_endpoint_is_unknown_not_inferred() -> None:
    result = extract(
        """import requests
url = "https://api.test/items"
requests.get(url, timeout=1)
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    call = result.calls[0]
    assert call.endpoint.status is FactStatus.UNKNOWN
    assert call.endpoint.value is None


def test_broad_exception_handler_is_ambiguous() -> None:
    result = extract(
        """import requests
for attempt in range(2):
    try:
        requests.get("https://api.test/items", timeout=1)
    except Exception:
        continue
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    loop = result.retry_loops[0]
    assert loop.triggers.status is FactStatus.AMBIGUOUS
    assert loop.max_attempts.status is FactStatus.KNOWN


def test_unknown_headers_variable_makes_key_presence_unknown() -> None:
    result = extract(
        """import requests
def send(headers):
    return requests.post("https://api.test/pay", headers=headers, timeout=1)
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    call = result.calls[0]
    assert call.idempotency_key.status is FactStatus.UNKNOWN
    assert call.idempotency_key.value is None


def test_unknown_key_generator_inside_loop_is_ambiguous_about_reuse() -> None:
    result = extract(
        """import requests
for attempt in range(2):
    key = make_key()
    try:
        requests.post(
            "https://api.test/pay",
            headers={"Idempotency-Key": key},
            timeout=1,
        )
    except requests.exceptions.Timeout:
        continue
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    info = result.calls[0].idempotency_key.value
    assert info is not None
    assert info.generated_inside_retry.value is True
    assert info.reused_across_attempts.status is FactStatus.UNKNOWN


def test_tenacity_decorator_is_explicitly_unsupported() -> None:
    result = extract(
        """from tenacity import retry, stop_after_attempt
import requests
@retry(stop=stop_after_attempt(3))
def call():
    return requests.get("https://api.test/items", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert len(result.calls) == 1
    assert result.retry_loops == ()
    codes = {warning.code for warning in result.warnings}
    assert "unsupported.retry_library" in codes
    assert "unsupported.retry_decorator" in codes


def test_urllib3_and_adapter_retry_policy_is_explicitly_unsupported() -> None:
    result = extract(
        """import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(total=3)))
session.get("https://api.test/items", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    codes = {warning.code for warning in result.warnings}
    assert "unsupported.retry_policy" in codes
    assert "unsupported.adapter_retry_policy" in codes
    assert "unsupported.urllib3_retry" in codes


def test_while_loop_retry_is_explicitly_unsupported() -> None:
    result = extract(
        """import requests
attempts = 0
while attempts < 3:
    try:
        requests.get("https://api.test/items", timeout=1)
    except requests.exceptions.Timeout:
        attempts += 1
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.retry_loops == ()
    assert "unsupported.retry_loop" in {warning.code for warning in result.warnings}


def test_session_level_headers_make_key_presence_unknown() -> None:
    result = extract(
        """import requests
session = requests.Session()
session.headers["Idempotency-Key"] = "abc"
session.post("https://api.test/pay", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.PARTIAL
    call = result.calls[0]
    assert call.idempotency_key.status is FactStatus.UNKNOWN
    assert "session-level headers" in (call.idempotency_key.reason or "")


def test_route_named_retry_is_not_mistaken_for_retry_decorator() -> None:
    result = extract(
        """from flask import Flask
app = Flask(__name__)
@app.route("/retry")
def retry_view():
    return "ok"
"""
    )

    assert result.status is ExtractionStatus.NO_HTTP_CALLS
    assert result.warnings == ()


def test_unrecognized_http_like_receiver_is_marked() -> None:
    result = extract(
        """class Client:
    def send(self):
        self._session.post(
            f"{BASE_URL}/pay",
            json={"amount_cents": 1},
            timeout=1,
        )
"""
    )

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert result.calls == ()
    assert "unsupported.http_call" in {warning.code for warning in result.warnings}


def test_unsupported_http_client_is_marked() -> None:
    result = extract(
        """import httpx
httpx.post("https://api.test/items", timeout=1)
"""
    )

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert result.calls == ()
    codes = {warning.code for warning in result.warnings}
    assert "unsupported.http_client" in codes
    assert "unsupported.http_call" in codes


def test_no_http_calls_is_not_reported_as_supported() -> None:
    result = extract("value = 1\n")

    assert result.status is ExtractionStatus.NO_HTTP_CALLS
    assert result.calls == ()


def test_parse_error_is_reported_with_location() -> None:
    result = extract("def broken(:\n", filename="broken.py")

    assert result.status is ExtractionStatus.PARSE_ERROR
    assert result.warnings[0].code == "parse_error"
    assert result.warnings[0].location is not None
    assert result.warnings[0].location.file == "broken.py"


def test_kwargs_make_timeout_and_headers_unknown() -> None:
    result = extract(
        """import requests
requests.get("https://api.test/items", **options)
"""
    )

    call = result.calls[0]
    assert result.status is ExtractionStatus.PARTIAL
    assert call.timeout.status is FactStatus.UNKNOWN
    assert call.idempotency_key.status is FactStatus.UNKNOWN


def test_extract_from_file_records_the_real_path(tmp_path) -> None:
    path = tmp_path / "client.py"
    path.write_text(
        """import requests
requests.get("https://api.test/items", timeout=1)
""",
        encoding="utf-8",
    )

    result = extract_from_file(path)

    assert result.file == str(path)
    assert result.calls[0].location.file == str(path)
    assert result.status is ExtractionStatus.SUPPORTED


def test_result_is_json_serializable() -> None:
    result = extract(
        """import requests
requests.get("https://api.test/items", timeout=1)
"""
    )

    payload = json.loads(result.to_json())
    assert payload["file"] == "example.py"
    assert payload["status"] == "supported"
    assert payload["calls"][0]["timeout"]["value"] == 1
    assert payload["calls"][0]["method"]["value"] == "GET"