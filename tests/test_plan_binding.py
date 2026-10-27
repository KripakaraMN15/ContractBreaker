"""Tests for plan-bound approval and CLI source containment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from contractbreaker.cli import main
from contractbreaker.plan import (
    PLAN_SCHEMA_VERSION,
    PlanBindingError,
    build_plan,
    load_plan,
    normalize_constraints,
    source_digest,
    verify_plan,
    write_plan,
)
from contractbreaker.workflow import WorkflowRequest, run_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
BUGGY = REPO_ROOT / "benchmark" / "development" / "dev_lost_for_buggy.py"
REQUEST = "duplicate payment after a lost response"

CONSTRAINTS = normalize_constraints(
    retry_statuses=None,
    server_idempotency=None,
    server_error_status=None,
    execution_timeout=None,
    llm_fallback=False,
)


def make_plan(path: Path, *, root: Path | None = None, runnable: bool = True):
    return build_plan(
        tool_version="0.1.0",
        scenario="lost-response",
        scenario_source="request_text",
        source_path=path.resolve(),
        source_sha256=source_digest(path),
        constraints=CONSTRAINTS,
        allowed_root=root.resolve() if root is not None else None,
        readiness={
            "scenario": "lost-response",
            "runnable": runnable,
            "blockers": [],
        },
    )


def verify(plan, *, path=BUGGY, root=None, scenario="lost-response", **overrides):
    constraints = dict(CONSTRAINTS)
    constraints.update(overrides.pop("constraints", {}))
    verify_plan(
        plan,
        tool_version=overrides.pop("tool_version", "0.1.0"),
        scenario=scenario,
        source_path=path.resolve(),
        source_sha256=source_digest(path),
        constraints=constraints,
        allowed_root=root.resolve() if root is not None else None,
    )


# --- plan artifact integrity ----------------------------------------------


def test_plan_round_trips_and_verifies(tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    write_plan(plan_path, make_plan(BUGGY))

    loaded = load_plan(plan_path)

    assert loaded["plan_schema_version"] == PLAN_SCHEMA_VERSION
    assert loaded["source"]["sha256"] == source_digest(BUGGY)
    verify(loaded)


def test_modified_plan_file_is_rejected(tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({**make_plan(BUGGY), "scenario": "retryable-error"}),
        encoding="utf-8",
    )

    with pytest.raises(PlanBindingError, match="digest"):
        load_plan(plan_path)


def test_plan_without_binding_is_rejected(tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    payload = make_plan(BUGGY)
    payload.pop("binding")
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PlanBindingError, match="binding"):
        load_plan(plan_path)


def test_missing_plan_file_is_rejected(tmp_path) -> None:
    with pytest.raises(PlanBindingError, match="could not be read"):
        load_plan(tmp_path / "absent.json")


# --- stale or altered investigations --------------------------------------


def test_source_content_change_is_rejected(tmp_path) -> None:
    copy = tmp_path / "client.py"
    copy.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    plan = make_plan(copy)
    copy.write_text(
        copy.read_text(encoding="utf-8") + "\n# changed after review\n",
        encoding="utf-8",
    )

    with pytest.raises(PlanBindingError, match="source content changed"):
        verify(plan, path=copy)


def test_different_source_path_is_rejected(tmp_path) -> None:
    other = tmp_path / "other.py"
    other.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    plan = make_plan(BUGGY)

    with pytest.raises(PlanBindingError, match="source path differs"):
        verify(plan, path=other)


def test_different_scenario_is_rejected() -> None:
    plan = make_plan(BUGGY)

    with pytest.raises(PlanBindingError, match="scenario differs"):
        verify(plan, scenario="retryable-error")


@pytest.mark.parametrize(
    "override,label",
    [
        ({"retry_statuses": [503]}, "retry_statuses"),
        ({"server_idempotency": "off"}, "server_idempotency"),
        ({"server_error_status": 503}, "server_error_status"),
        ({"execution_timeout": 30.0}, "execution_timeout"),
        ({"llm_fallback": True}, "llm_fallback"),
    ],
)
def test_changed_execution_option_is_rejected(override, label) -> None:
    plan = make_plan(BUGGY)

    with pytest.raises(PlanBindingError, match=label):
        verify(plan, constraints=override)


def test_different_allowed_root_is_rejected(tmp_path) -> None:
    plan = make_plan(BUGGY, root=REPO_ROOT)

    with pytest.raises(PlanBindingError, match="allowed root differs"):
        verify(plan, root=tmp_path)


def test_different_tool_version_is_rejected() -> None:
    plan = make_plan(BUGGY)

    with pytest.raises(PlanBindingError, match="different tool version"):
        verify(plan, tool_version="9.9.9")


def test_non_runnable_plan_cannot_be_approved() -> None:
    plan = make_plan(BUGGY, runnable=False)

    with pytest.raises(PlanBindingError, match="not runnable"):
        verify(plan)


# --- CLI approval binding --------------------------------------------------


def workflow_args(*extra: str) -> list[str]:
    return [
        "workflow",
        "--request",
        REQUEST,
        "--source",
        str(BUGGY),
        *extra,
    ]


def test_approve_without_plan_is_rejected(capsys) -> None:
    code = main(workflow_args("--approve", "--format", "json"))
    captured = capsys.readouterr()

    assert code == 3
    assert "--plan" in captured.err
    assert captured.out == ""


def test_plan_without_approve_is_rejected(capsys, tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    write_plan(plan_path, make_plan(BUGGY))

    code = main(workflow_args("--plan", str(plan_path), "--format", "json"))
    captured = capsys.readouterr()

    assert code == 3
    assert "--approve" in captured.err


def test_plan_out_requires_plan_only(capsys, tmp_path) -> None:
    code = main(
        workflow_args("--plan-out", str(tmp_path / "plan.json"), "--format", "json")
    )
    captured = capsys.readouterr()

    assert code == 3
    assert "--plan-only" in captured.err


def test_bound_plan_executes_the_reviewed_investigation(capsys, tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_code = main(
        workflow_args(
            "--plan-only", "--plan-out", str(plan_path), "--format", "json"
        )
    )
    capsys.readouterr()
    assert plan_code == 2
    assert plan_path.is_file()

    code = main(
        workflow_args("--approve", "--plan", str(plan_path), "--format", "json")
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert code == 1
    assert document["outcome"] == "executed"
    assert document["verdict"] == "confirmed"
    assert document["analysis"]["execution"]["observed_side_effects"] == 2


def test_approval_after_source_change_is_rejected(capsys, tmp_path) -> None:
    client = tmp_path / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    plan_path = tmp_path / "plan.json"

    base = [
        "workflow",
        "--request",
        REQUEST,
        "--source",
        str(client),
    ]
    assert main([*base, "--plan-only", "--plan-out", str(plan_path)]) == 2
    capsys.readouterr()

    client.write_text(
        client.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    code = main([*base, "--approve", "--plan", str(plan_path), "--format", "json"])
    captured = capsys.readouterr()

    assert code == 3
    assert "source content changed" in captured.err
    assert captured.out == ""


def test_approval_with_different_scenario_is_rejected(capsys, tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    assert main(
        workflow_args("--plan-only", "--plan-out", str(plan_path))
    ) == 2
    capsys.readouterr()

    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(BUGGY),
            "--scenario",
            "retryable-error",
            "--approve",
            "--plan",
            str(plan_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    assert "scenario differs" in captured.err


def test_approval_with_different_option_is_rejected(capsys, tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    assert main(
        workflow_args("--plan-only", "--plan-out", str(plan_path))
    ) == 2
    capsys.readouterr()

    code = main(
        workflow_args(
            "--approve", "--plan", str(plan_path), "--execution-timeout", "30"
        )
    )
    captured = capsys.readouterr()

    assert code == 3
    assert "execution_timeout" in captured.err


# --- CLI source containment ------------------------------------------------


def test_source_outside_allowed_root_is_rejected(capsys, tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    code = main(workflow_args("--plan-only", "--allowed-root", str(root)))
    captured = capsys.readouterr()

    assert code == 4
    assert "outside the allowed root" in captured.err


def test_path_traversal_is_rejected(capsys, tmp_path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    client = root / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")

    traversing = root / "sub" / ".." / "client.py"
    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(traversing),
            "--plan-only",
            "--allowed-root",
            str(root / "sub"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 4
    assert "outside the allowed root" in captured.err


def test_symlink_escape_is_rejected(capsys, tmp_path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    client = root / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    link = root / "sub" / "link.py"
    try:
        link.symlink_to(client)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(link),
            "--plan-only",
            "--allowed-root",
            str(root / "sub"),
        ]
    )
    captured = capsys.readouterr()

    assert code == 4
    assert "outside the allowed root" in captured.err


def test_junction_escape_is_rejected(capsys, tmp_path) -> None:
    """Windows junctions are reparse points like symlinks and must not escape."""

    if os.name != "nt":
        pytest.skip("directory junctions only exist on Windows")
    root = tmp_path / "root"
    inside = root / "inside"
    outside = root / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    (outside / "client.py").write_text(
        BUGGY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    jump = inside / "jump"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(jump), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0 or not jump.exists():
        pytest.skip("junction creation is not permitted in this environment")

    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(jump / "client.py"),
            "--plan-only",
            "--allowed-root",
            str(inside),
        ]
    )
    captured = capsys.readouterr()

    assert code == 4
    assert "outside the allowed root" in captured.err


def test_source_inside_allowed_root_is_allowed(capsys, tmp_path) -> None:
    root = tmp_path / "root"
    (root / "clients").mkdir(parents=True)
    client = root / "clients" / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")

    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(client),
            "--plan-only",
            "--allowed-root",
            str(root),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.out)["outcome"] == "planned"


def test_containment_is_optional_for_existing_use(capsys, tmp_path) -> None:
    client = tmp_path / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")

    code = main(
        [
            "workflow",
            "--request",
            REQUEST,
            "--source",
            str(client),
            "--plan-only",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.out)["outcome"] == "planned"


# --- approved bytes are the executed bytes ---------------------------------


FIXED_SOURCE = '''import uuid
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
'''


def test_execution_refuses_source_altered_after_verification(tmp_path) -> None:
    client = tmp_path / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    approved_digest = hashlib.sha256(client.read_bytes()).hexdigest()

    # The plan was verified against the digest above, then the file changed.
    # The altered client would be not_reproduced / 1 effect if it ever ran.
    client.write_text(FIXED_SOURCE, encoding="utf-8")

    state = run_workflow(
        WorkflowRequest(
            request=REQUEST,
            source=client,
            scenario="lost-response",
            approved=True,
            expected_source_sha256=approved_digest,
        )
    )

    assert state.outcome == "failed"
    assert state.verdict == "inconclusive"
    assert state.report["execution"]["occurred"] is False
    assert state.report["execution"]["failure"]["kind"] == "source_mismatch"


def test_bound_execution_runs_the_approved_bytes(tmp_path) -> None:
    client = tmp_path / "client.py"
    client.write_text(BUGGY.read_text(encoding="utf-8"), encoding="utf-8")
    approved_digest = hashlib.sha256(client.read_bytes()).hexdigest()

    state = run_workflow(
        WorkflowRequest(
            request=REQUEST,
            source=client,
            scenario="lost-response",
            approved=True,
            expected_source_sha256=approved_digest,
        )
    )

    assert state.outcome == "executed"
    assert state.verdict == "confirmed"
    assert state.report["execution"]["observed_side_effects"] == 2


def test_execution_pass_is_pinned_to_the_verified_scenario(
    capsys, tmp_path, monkeypatch
) -> None:
    import contractbreaker.cli as cli_module
    from contractbreaker.workflow import run_workflow as real_run_workflow

    calls = []

    def spy(request):
        calls.append(request)
        return real_run_workflow(request)

    monkeypatch.setattr(cli_module, "run_workflow", spy)

    plan_path = tmp_path / "plan.json"
    assert (
        main(workflow_args("--plan-only", "--plan-out", str(plan_path)))
        == 2
    )
    capsys.readouterr()

    assert main(workflow_args("--approve", "--plan", str(plan_path))) == 1
    capsys.readouterr()

    # plan-only planning, approve planning, approve execution.
    assert len(calls) == 3
    assert calls[0].scenario is None
    assert calls[1].scenario is None
    assert calls[2].scenario == "lost-response"
    assert calls[2].expected_source_sha256
