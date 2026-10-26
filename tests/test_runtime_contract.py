"""Tests for the narrow executable-source contract."""

from __future__ import annotations

from contractbreaker import extract_from_source
from contractbreaker.runtime_contract import check_runtime_contract

VALID_SOURCE = """import requests

for attempt in range(2):
    try:
        requests.post("http://service.test/payments", timeout=5)
    except requests.exceptions.Timeout:
        continue
"""


def contract(source: str):
    extraction = extract_from_source(source, filename="client.py")
    return check_runtime_contract(source, extraction)


def test_supports_module_level_requests_client() -> None:
    result = contract(VALID_SOURCE)

    assert result.supported is True
    assert result.blockers == ()
    assert result.entrypoint == "module"


def test_rejects_call_inside_function() -> None:
    result = contract(
        """import requests

def send():
    for attempt in range(2):
        requests.post("http://service.test/payments", timeout=5)
"""
    )

    assert result.supported is False
    assert any("module-level" in blocker for blocker in result.blockers)


def test_rejects_disallowed_import() -> None:
    result = contract(
        """import socket
import requests
requests.post("http://service.test/payments", timeout=5)
"""
    )

    assert result.supported is False
    assert any("not supported for execution" in blocker for blocker in result.blockers)


def test_rejects_requests_adapter_import() -> None:
    result = contract(
        """from requests.adapters import HTTPAdapter
import requests
requests.post("http://service.test/payments", timeout=5)
"""
    )

    assert result.supported is False
    assert any("not supported for execution" in blocker for blocker in result.blockers)


def test_rejects_forbidden_call() -> None:
    result = contract(
        """import requests
open("some-file")
requests.post("http://service.test/payments", timeout=5)
"""
    )

    assert result.supported is False
    assert any("call to open" in blocker for blocker in result.blockers)


def test_rejects_session_mount() -> None:
    result = contract(
        """import requests
session = requests.Session()
session.mount("http://", None)
session.post("http://service.test/payments", timeout=5)
"""
    )

    assert result.supported is False
    assert any("session.mount" in blocker for blocker in result.blockers)


def test_rejects_source_without_recognized_call() -> None:
    result = contract("value = 1\n")

    assert result.supported is False
    assert any("recognized requests call" in blocker for blocker in result.blockers)