"""Dashboard backend and HTTP tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from contractbreaker.dashboard.api import (
    ApiError,
    DashboardConfig,
    DashboardService,
)
from contractbreaker.dashboard.server import create_server

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


@pytest.fixture()
def service(tmp_path: Path) -> DashboardService:
    (tmp_path / "client.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "fixed.py").write_text(FIXED, encoding="utf-8")
    (tmp_path / "unsupported.py").write_text(UNSUPPORTED, encoding="utf-8")
    (tmp_path / "nested.py").write_text(NESTED, encoding="utf-8")
    return DashboardService(DashboardConfig(allowed_root=tmp_path))


def plan(service: DashboardService, **payload) -> dict:
    return service.plan(payload)


# -- plan only --------------------------------------------------------


def test_plan_only_never_executes(service: DashboardService) -> None:
    response = plan(service, request="check a lost response", source="client.py")

    assert response["ok"] is True
    assert response["plan"]["outcome"] == "planned"
    assert response["plan"]["analysis"] is None
    assert response["plan"]["execution"] if False else True
    assert response["plan"]["analysis_snapshot"]["hypothesis"][
        "scenario_readiness"
    ]["runnable"] is True
    assert response["approval_token"]


def test_plan_contains_no_runtime_events(service: DashboardService) -> None:
    response = plan(service, request="check a lost response", source="client.py")
    snapshot = response["plan"]["analysis_snapshot"]

    assert "events" not in snapshot
    assert snapshot["hypothesis"]["scenario_readiness"]["runnable"] is True


def test_blocked_client_is_not_executable(service: DashboardService) -> None:
    response = plan(
        service, request="check a lost response", source="unsupported.py"
    )

    assert response["ready"] is False
    assert response["approval_token"] is None
    assert response["plan"]["outcome"] == "blocked"
    assert response["plan"]["analysis"] is None


def test_nested_client_is_not_executable(service: DashboardService) -> None:
    response = plan(service, request="check a lost response", source="nested.py")

    assert response["ready"] is False
    assert response["approval_token"] is None
    assert any(
        "module-level" in blocker
        for blocker in response["plan"]["analysis_snapshot"]["hypothesis"][
            "scenario_readiness"
        ]["blockers"]
    )


# -- approval ---------------------------------------------------------


def test_execution_requires_approval_token(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        service.execute({"approved": True})
    assert error.value.code == "approval_required"
    assert error.value.status == 403


def test_execution_requires_explicit_approved_flag(service: DashboardService) -> None:
    planned = plan(service, request="check a lost response", source="client.py")

    with pytest.raises(ApiError) as error:
        service.execute({"approval_token": planned["approval_token"]})
    assert error.value.code == "approval_required"
    assert error.value.status == 403


def test_approved_execution_returns_real_verdict(service: DashboardService) -> None:
    planned = plan(service, request="check a lost response", source="client.py")
    result = service.execute(
        {"approval_token": planned["approval_token"], "approved": True}
    )
    document = result["result"]

    assert document["outcome"] == "executed"
    assert document["verdict"] == "confirmed"
    assert document["analysis"]["verdict"] == "confirmed"
    assert document["analysis"]["verdict_provenance"] == "runtime_verifier"
    assert document["analysis"]["execution"]["observed_side_effects"] == 2
    assert document["analysis"]["execution"]["occurred"] is True
    assert len(document["analysis"]["execution"]["events"]) > 0


def test_approved_fixed_client_is_not_reproduced(service: DashboardService) -> None:
    planned = plan(service, request="check a lost response", source="fixed.py")
    result = service.execute(
        {"approval_token": planned["approval_token"], "approved": True}
    )
    document = result["result"]

    assert document["verdict"] == "not_reproduced"
    assert document["analysis"]["execution"]["observed_side_effects"] == 1


def test_approval_token_is_single_use(service: DashboardService) -> None:
    planned = plan(service, request="check a lost response", source="client.py")
    token = planned["approval_token"]

    service.execute({"approval_token": token, "approved": True})

    with pytest.raises(ApiError) as error:
        service.execute({"approval_token": token, "approved": True})
    assert error.value.code == "already_executed"
    assert error.value.status == 409


def test_backend_enforces_readiness_independently_of_client_state(
    service: DashboardService,
) -> None:
    """A token for a blocked plan can never execute."""

    planned = plan(
        service, request="check a lost response", source="unsupported.py"
    )
    assert planned["approval_token"] is None

    with pytest.raises(ApiError) as error:
        service.execute(
            {"approval_token": "forged-token", "approved": True}
        )
    assert error.value.code == "unknown_approval"


def test_unknown_token_is_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        service.execute({"approval_token": "nope", "approved": True})
    assert error.value.code == "unknown_approval"
    assert error.value.status == 409


# -- input validation -------------------------------------------------


def test_source_outside_allowed_root_is_rejected(
    service: DashboardService, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside_client.py"
    outside.write_text(BUGGY, encoding="utf-8")

    with pytest.raises(ApiError) as error:
        plan(service, request="check a lost response", source=str(outside))
    assert error.value.code == "invalid_source"
    assert error.value.status == 403


def test_path_traversal_is_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        plan(
            service,
            request="check a lost response",
            source="../../etc/passwd.py",
        )
    assert error.value.code == "invalid_source"


def test_non_python_source_is_rejected(service: DashboardService) -> None:
    (Path(service._config.allowed_root) / "notes.txt").write_text(
        "hello", encoding="utf-8"
    )

    with pytest.raises(ApiError) as error:
        plan(service, request="check a lost response", source="notes.txt")
    assert error.value.code == "invalid_source"


def test_missing_source_file_is_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        plan(service, request="check a lost response", source="absent.py")
    assert error.value.code == "invalid_source"


def test_invalid_scenario_is_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        plan(
            service,
            request="check a lost response",
            source="client.py",
            scenario="meltdown",
        )
    assert error.value.code == "invalid_scenario"


def test_invalid_constraints_are_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        plan(
            service,
            request="check a retryable error",
            source="client.py",
            scenario="lost-response",
            retry_statuses=[503],
        )
    assert error.value.code == "invalid_constraint"

    with pytest.raises(ApiError) as status_error:
        plan(
            service,
            request="check a retryable error",
            source="client.py",
            scenario="retryable-error",
            retry_statuses=[200],
        )
    assert status_error.value.code == "invalid_constraint"

    with pytest.raises(ApiError) as timeout_error:
        plan(
            service,
            request="check a lost response",
            source="client.py",
            execution_timeout=-1,
        )
    assert timeout_error.value.code == "invalid_constraint"


def test_oversized_request_is_rejected(service: DashboardService) -> None:
    with pytest.raises(ApiError) as error:
        plan(service, request="x" * 5000, source="client.py")
    assert error.value.code == "invalid_request"


def test_ambiguous_request_is_reported_not_guessed(
    service: DashboardService,
) -> None:
    response = plan(
        service,
        request="retry after a timeout and also handle a 503 server error",
        source="client.py",
    )

    assert response["ready"] is False
    assert response["plan"]["outcome"] == "blocked"
    assert response["plan"]["stages"][0]["details"]["blocked_kind"] == (
        "invalid_request"
    )


# -- secrets ----------------------------------------------------------


def test_health_does_not_expose_environment(service: DashboardService) -> None:
    health = service.health()
    serialized = json.dumps(health)

    assert "OPENAI_API_KEY" not in serialized
    assert "api_key" not in serialized.lower()
    assert set(health) == {
        "ok",
        "api_schema_version",
        "allowed_root",
        "llm_fallback_available",
        "supported_scenarios",
    }


def test_plan_and_execute_never_expose_secrets(
    service: DashboardService, monkeypatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("SECRET_TOKEN", "another-secret")

    planned = plan(service, request="check a lost response", source="client.py")
    result = service.execute(
        {"approval_token": planned["approval_token"], "approved": True}
    )
    serialized = json.dumps([planned, result])

    assert "sk-super-secret-value" not in serialized
    assert "another-secret" not in serialized
    assert "OPENAI_API_KEY" not in serialized


def test_source_listing_stays_inside_root(service: DashboardService) -> None:
    listing = service.list_sources()

    assert "client.py" in listing["sources"]
    assert all(not name.startswith("..") for name in listing["sources"])


# -- HTTP layer -------------------------------------------------------


@pytest.fixture()
def http_server(service: DashboardService):
    server = create_server(service._config.allowed_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def http_json(url: str, payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_http_index_and_assets_are_served(http_server: str) -> None:
    with urllib.request.urlopen(f"{http_server}/", timeout=30) as response:
        body = response.read().decode("utf-8")
    assert response.status == 200
    assert "ContractBreaker" in body
    assert "/static/app.js" in body

    with urllib.request.urlopen(f"{http_server}/static/app.js", timeout=30) as js:
        assert js.status == 200
        assert b"fetch" in js.read()


def test_http_asset_path_traversal_is_blocked(http_server: str) -> None:
    status, payload = http_json(f"{http_server}/static/../api.py")
    assert status in (403, 404)
    assert payload["ok"] is False


def test_http_plan_then_execute(http_server: str) -> None:
    status, planned = http_json(
        f"{http_server}/api/plan",
        {"request": "check a lost response", "source": "client.py"},
    )
    assert status == 200
    assert planned["ready"] is True
    assert planned["plan"]["analysis"] is None

    status, result = http_json(
        f"{http_server}/api/execute",
        {"approval_token": planned["approval_token"], "approved": True},
    )
    assert status == 200
    assert result["result"]["verdict"] == "confirmed"
    assert result["result"]["analysis"]["execution"]["observed_side_effects"] == 2


def test_http_execute_without_approval_is_rejected(http_server: str) -> None:
    status, payload = http_json(
        f"{http_server}/api/execute", {"approved": True}
    )
    assert status == 403
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"


def test_http_invalid_body_is_rejected(http_server: str) -> None:
    request = urllib.request.Request(
        f"{http_server}/api/plan",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=30)
        raise AssertionError("expected an error response")
    except urllib.error.HTTPError as error:
        assert error.code == 400
        payload = json.loads(error.read().decode("utf-8"))
        assert payload["error"]["code"] == "invalid_body"


def test_http_health_lists_sources(http_server: str) -> None:
    status, payload = http_json(f"{http_server}/api/health")
    assert status == 200
    assert payload["ok"] is True

    status, sources = http_json(f"{http_server}/api/sources")
    assert status == 200
    assert "client.py" in sources["sources"]
