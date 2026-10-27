"""Milestone 10 tests: runtime idempotency-key stability and dedup provenance."""

from __future__ import annotations

import json
import re
from pathlib import Path

import flask
import pytest
import requests

from contractbreaker import (
    Event,
    EventKind,
    EventLog,
    FaultInjectionAdapter,
    FaultPlan,
    check_idempotency_key_stability,
    verify_duplicate_side_effects,
)
from contractbreaker.idempotency import (
    OUTCOME_ABSENT,
    OUTCOME_CHANGING,
    OUTCOME_INSUFFICIENT_EVIDENCE,
    OUTCOME_STABLE,
)
from contractbreaker.pipeline import AnalysisOptions, analyze

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = REPO_ROOT / "benchmark" / "development"

HEX16 = re.compile(r"^[0-9a-f]{16}$")

STABLE_LITERAL = '''import requests

idempotency_key = "fixed-key"
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
'''

CHANGING_KEY = '''import uuid
import requests

for attempt in range(2):
    idempotency_key = str(uuid.uuid4())
    try:
        requests.post(
            "http://service.test/payments",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
'''

NO_KEY = '''import requests

for attempt in range(2):
    try:
        requests.post(
            "http://service.test/payments",
            json={"amount_cents": 1999},
            timeout=5,
        )
    except requests.exceptions.Timeout:
        continue
'''

UNRESOLVED_REUSE = '''import requests

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
'''


def build_trace(
    *,
    fingerprints: tuple[str | None, ...] = ("aaaa", "aaaa"),
    present: tuple[bool, ...] | None = None,
    attempt_ids: tuple[str, ...] | None = None,
    operation_ids: tuple[str, ...] | None = None,
    paths: tuple[str, ...] | None = None,
    include_presence: bool = True,
) -> list[Event]:
    """Build a keyed request trace without going through the runtime."""

    count = len(fingerprints)
    presence = present or tuple(fp is not None for fp in fingerprints)
    ids = attempt_ids or tuple(f"attempt-{i + 1}" for i in range(count))
    operations = operation_ids or tuple("op-1" for _ in range(count))
    resolved_paths = paths or tuple("/payments" for _ in range(count))

    events: list[Event] = []
    seq = 1
    for index in range(count):
        data: dict[str, object] = {"path": resolved_paths[index]}
        if include_presence:
            data["idempotency_key_present"] = presence[index]
            data["idempotency_key_fingerprint"] = fingerprints[index]
        events.append(
            Event(
                seq=seq,
                kind=EventKind.REQUEST_SENT,
                attempt_id=ids[index],
                operation_id=operations[index],
                data=data,
            )
        )
        seq += 1
    return events


# --- observed key outcomes -------------------------------------------------


def test_same_key_on_every_attempt_is_stable() -> None:
    result = check_idempotency_key_stability(build_trace())

    assert result.outcome == OUTCOME_STABLE
    assert result.stable is True
    assert result.distinct_key_count == 1
    assert [a["attempt_id"] for a in result.attempts] == [
        "attempt-1",
        "attempt-2",
    ]


def test_different_key_per_attempt_is_changing() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=("aaaa", "bbbb"))
    )

    assert result.outcome == OUTCOME_CHANGING
    assert result.distinct_key_count == 2
    assert "2 distinct" in result.reason


def test_no_key_is_absent() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=(None, None))
    )

    assert result.outcome == OUTCOME_ABSENT
    assert result.distinct_key_count == 0
    assert all(attempt["key_fingerprint"] is None for attempt in result.attempts)


def test_key_presence_changing_across_attempts_is_changing() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=("aaaa", None))
    )

    assert result.outcome == OUTCOME_CHANGING
    assert "presence changed" in result.reason


def test_single_keyed_attempt_cannot_establish_stability() -> None:
    result = check_idempotency_key_stability(build_trace(fingerprints=("aaaa",)))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "can not be compared" in result.reason


# --- insufficient evidence -------------------------------------------------


def test_empty_trace_is_insufficient() -> None:
    result = check_idempotency_key_stability([])

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE


def test_keyed_attempt_without_fingerprint_is_insufficient() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=(None, "bbbb"), present=(True, True))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "no key fingerprint" in result.reason


def test_attempt_without_presence_evidence_is_insufficient() -> None:
    result = check_idempotency_key_stability(build_trace(include_presence=False))

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "presence" in result.reason


def test_duplicate_attempt_id_is_insufficient() -> None:
    result = check_idempotency_key_stability(
        build_trace(attempt_ids=("attempt-1", "attempt-1"))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE


def test_multiple_operations_are_insufficient() -> None:
    result = check_idempotency_key_stability(
        build_trace(operation_ids=("op-1", "op-2"))
    )

    assert result.outcome == OUTCOME_INSUFFICIENT_EVIDENCE
    assert "more than one logical operation" in result.reason


def test_unrelated_paths_are_excluded() -> None:
    result = check_idempotency_key_stability(
        build_trace(
            fingerprints=("aaaa", "aaaa", None),
            paths=("/payments", "/payments", "/ping"),
        )
    )

    assert result.outcome == OUTCOME_STABLE
    assert result.distinct_key_count == 1


# --- static expectation versus runtime reality -----------------------------


def test_matching_static_expectation_is_reported() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=("aaaa", "aaaa")),
        static_key_present=True,
        static_key_reused=True,
    )

    assert result.matches_static_expectation is True
    assert result.static_expectation == {
        "key_present": True,
        "reused_across_attempts": True,
        "provenance": "derived_from_extraction",
    }


def test_runtime_change_contradicts_a_stable_static_expectation() -> None:
    """The static prediction is stable; the observed keys are not."""

    result = check_idempotency_key_stability(
        build_trace(fingerprints=("aaaa", "bbbb")),
        static_key_present=True,
        static_key_reused=True,
    )

    assert result.outcome == OUTCOME_CHANGING
    assert result.matches_static_expectation is False
    assert "contradicts the static expectation" in result.reason


def test_unexpected_key_when_static_says_absent_is_reported() -> None:
    result = check_idempotency_key_stability(
        build_trace(fingerprints=("aaaa", "aaaa")),
        static_key_present=False,
    )

    assert result.matches_static_expectation is False


def test_provenance_keeps_observation_and_prediction_separate() -> None:
    payload = check_idempotency_key_stability(
        build_trace(),
        static_key_present=True,
        static_key_reused=True,
    ).to_dict()

    assert payload["provenance"] == {
        "outcome": "runtime_check",
        "observed_keys": "runtime_observation",
        "static_expectation": "derived_from_extraction",
    }
    assert payload["static_expectation"]["provenance"] == (
        "derived_from_extraction"
    )


def test_fingerprints_are_not_raw_key_values() -> None:
    from contractbreaker.observations import key_fingerprint

    app = flask.Flask(__name__)

    @app.post("/payments")
    def payments():
        return {"ok": True}, 201

    log = EventLog()
    adapter = FaultInjectionAdapter(
        app, log, FaultPlan(drop_response_on_attempts=frozenset())
    )
    session = requests.Session()
    session.mount("http://", adapter)
    session.post(
        "http://service.test/payments",
        headers={"Idempotency-Key": "super-secret-key"},
        json={"amount_cents": 1},
        timeout=1,
    )

    sent = next(
        event
        for event in log.events
        if event.kind is EventKind.REQUEST_SENT
    )
    assert sent.data["idempotency_key_present"] is True
    assert sent.data["idempotency_key_fingerprint"] == key_fingerprint(
        "super-secret-key"
    )
    assert "super-secret-key" not in str(sent.data)


# --- key mismatch alone does not confirm a duplicate ------------------------


def test_changing_key_without_a_second_commit_is_not_confirmed() -> None:
    """A key mismatch is reported, but the verdict stays with the verifier."""

    events = [
        Event(
            1,
            EventKind.REQUEST_SENT,
            "attempt-1",
            "op-1",
            {
                "path": "/payments",
                "idempotency_key_present": True,
                "idempotency_key_fingerprint": "aaaaaaaaaaaaaaaa",
            },
        ),
        Event(2, EventKind.SIDE_EFFECT_COMMITTED, "attempt-1", "op-1", {"side_effect_id": "effect-1"}),
        Event(3, EventKind.RESPONSE_DROPPED, "attempt-1", "op-1", {"status_code": 201}),
        Event(
            4,
            EventKind.REQUEST_SENT,
            "attempt-2",
            "op-1",
            {
                "path": "/payments",
                "idempotency_key_present": True,
                "idempotency_key_fingerprint": "bbbbbbbbbbbbbbbb",
            },
        ),
        Event(5, EventKind.SCENARIO_END, None, None, {"outcome": "client_completed"}),
    ]

    keys = check_idempotency_key_stability(events)
    verdict = verify_duplicate_side_effects(events, side_effects=1)

    assert keys.outcome == OUTCOME_CHANGING
    assert verdict.verdict.value == "inconclusive"


# --- end-to-end ------------------------------------------------------------


def analyze_source(tmp_path: Path, name: str, source: str, **options) -> dict:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return analyze(
        AnalysisOptions(source=path, scenario="lost-response", **options)
    )


def test_stable_key_with_explicit_dedup_on_is_not_reproduced(tmp_path) -> None:
    report = analyze_source(
        tmp_path,
        "stable.py",
        STABLE_LITERAL,
        server_idempotency="on",
    )

    assert report["verdict"] == "not_reproduced"
    assert report["execution"]["observed_side_effects"] == 1
    keys = report["execution"]["idempotency_key"]
    assert keys["outcome"] == OUTCOME_STABLE
    assert keys["matches_static_expectation"] is True
    dedup = report["execution"]["server_deduplication"]
    assert dedup == {
        "enabled": True,
        "provenance": "cli",
        "inferred": False,
        "observed_replay_count": 1,
        "reason": "server deduplication was configured explicitly on",
    }


def test_stable_key_with_explicit_dedup_off_still_confirms(tmp_path) -> None:
    report = analyze_source(
        tmp_path,
        "stable.py",
        STABLE_LITERAL,
        server_idempotency="off",
    )

    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    assert report["execution"]["idempotency_key"]["outcome"] == OUTCOME_STABLE
    dedup = report["execution"]["server_deduplication"]
    assert dedup["enabled"] is False
    assert dedup["provenance"] == "cli"
    assert dedup["observed_replay_count"] == 0


def test_changing_key_with_explicit_dedup_on_is_confirmed(tmp_path) -> None:
    report = analyze_source(
        tmp_path,
        "changing.py",
        CHANGING_KEY,
        server_idempotency="on",
    )

    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    keys = report["execution"]["idempotency_key"]
    assert keys["outcome"] == OUTCOME_CHANGING
    assert keys["distinct_key_count"] == 2
    assert keys["matches_static_expectation"] is True
    dedup = report["execution"]["server_deduplication"]
    assert dedup["enabled"] is True
    assert dedup["observed_replay_count"] == 0


def test_missing_key_with_explicit_dedup_off_is_confirmed(tmp_path) -> None:
    report = analyze_source(
        tmp_path,
        "keyless.py",
        NO_KEY,
        server_idempotency="off",
    )

    assert report["verdict"] == "confirmed"
    assert report["execution"]["observed_side_effects"] == 2
    assert report["execution"]["idempotency_key"]["outcome"] == OUTCOME_ABSENT
    assert report["execution"]["server_deduplication"]["enabled"] is False


def test_auto_mode_is_marked_inferred_and_disclosed(tmp_path) -> None:
    report = analyze_source(
        tmp_path,
        "stable.py",
        STABLE_LITERAL,
        server_idempotency="auto",
    )

    assert report["verdict"] == "not_reproduced"
    dedup = report["execution"]["server_deduplication"]
    assert dedup["enabled"] is True
    assert dedup["provenance"] == "inferred_from_client_facts"
    assert dedup["inferred"] is True
    assert "inferred" in dedup["reason"]
    assert any(
        "inferred from the client's key presence" in limitation
        for limitation in report["limitations"]
    )


def test_report_never_contains_the_raw_key(tmp_path) -> None:
    # A literal key makes the assertion exact: that string must not appear.
    report = analyze_source(
        tmp_path,
        "stable.py",
        STABLE_LITERAL,
        server_idempotency="on",
    )

    serialized = json.dumps(report)
    assert "fixed-key" not in serialized
    keys = report["execution"]["idempotency_key"]
    fingerprints = [a["key_fingerprint"] for a in keys["attempts"]]
    assert fingerprints
    for fingerprint in fingerprints:
        assert HEX16.match(fingerprint)
        assert fingerprint in serialized
    replay = [
        event
        for event in report["execution"]["events"]
        if event["kind"] == "idempotent_replay"
    ]
    assert replay and "fixed-key" not in json.dumps(replay)


def test_unresolved_key_reuse_is_blocked_and_has_no_key_evidence(tmp_path) -> None:
    report = analyze_source(tmp_path, "unresolved.py", UNRESOLVED_REUSE)

    assert report["verdict"] == "inconclusive"
    assert report["execution"]["occurred"] is False
    assert report["execution"]["idempotency_key"] is None
    assert any(
        "reuse" in blocker
        for blocker in report["hypothesis"]["scenario_readiness"]["blockers"]
    )


@pytest.mark.parametrize(
    "fixture,expected_verdict,expected_key,server_idempotency",
    [
        ("dev_fresh_key.py", "confirmed", OUTCOME_CHANGING, "auto"),
        ("dev_key_stable_dedup_off.py", "confirmed", OUTCOME_STABLE, "off"),
        ("dev_key_stable_dedup_on.py", "not_reproduced", OUTCOME_STABLE, "on"),
        ("dev_lost_for_buggy.py", "confirmed", OUTCOME_ABSENT, "off"),
    ],
)
def test_benchmark_fixtures_match_runtime_key_behaviour(
    tmp_path, fixture, expected_verdict, expected_key, server_idempotency
) -> None:
    report = analyze(
        AnalysisOptions(
            source=DEVELOPMENT / fixture,
            server_idempotency=server_idempotency,
        )
    )

    assert report["verdict"] == expected_verdict
    assert report["execution"]["idempotency_key"]["outcome"] == expected_key
