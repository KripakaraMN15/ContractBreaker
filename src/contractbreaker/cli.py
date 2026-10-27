"""Developer CLI for the deterministic ContractBreaker pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .benchmark import (
    EXIT_BENCHMARK_ERROR,
    BenchmarkDatasetError,
    benchmark_exit_code,
    benchmark_report_to_json,
    render_benchmark_text,
    run_benchmark,
    write_benchmark_report,
)
from .pipeline import (
    SUPPORTED_SCENARIOS,
    SUPPORTED_SERVER_IDEMPOTENCY,
    SUPPORTED_TIMEOUT_MODES,
    AnalysisOptions,
    AnalysisUsageError,
    SourceReadError,
    analyze,
)
from .timeout_contract import TIMEOUT_INVARIANTS
from .report import (
    EXIT_INVALID_ARGS,
    EXIT_UNSUPPORTED_INPUT,
    exit_code_for_report,
    render_text,
    report_to_json,
    write_report,
)
from .dashboard.server import serve as serve_dashboard
from .version import __version__
from .plan import (
    PlanBindingError,
    build_plan,
    load_plan,
    normalize_constraints,
    source_digest,
    verify_plan,
    write_plan,
)
from .workflow import (
    SUPPORTED_WORKFLOW_SCENARIOS,
    WorkflowRequest,
    WorkflowState,
    render_workflow_text,
    run_workflow,
    workflow_exit_code,
)

_ANALYZE_EPILOG = """analyze exit codes:
  0  analysis completed: not_reproduced
  1  analysis completed: confirmed
  2  analysis completed: inconclusive or not runnable
  3  invalid CLI arguments
  4  unsupported/invalid input or report write failure
"""

_BENCHMARK_EPILOG = """benchmark exit codes:
  0  benchmark completed with all labeled cases matched
  1  benchmark completed with one or more mismatches
  2  benchmark dataset or execution could not be completed
  3  invalid CLI arguments
"""

_DASHBOARD_EPILOG = """dashboard exit codes:
  0  dashboard server stopped normally
  2  dashboard server could not start
  3  invalid CLI arguments
"""

_WORKFLOW_EPILOG = """workflow exit codes:
  0  executed; runtime verdict not_reproduced
  1  executed; runtime verdict confirmed
  2  planned, approval required, not runnable, or inconclusive
  3  invalid or underspecified request
  4  unsupported request or input (including unsupported source)
"""

_TOP_LEVEL_EPILOG = (
    _DASHBOARD_EPILOG
    + "\n\n"
    + _WORKFLOW_EPILOG
    + "\n\n"
    + _BENCHMARK_EPILOG
    + "\n\n"
    + _ANALYZE_EPILOG
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID_ARGS, f"{self.prog}: error: {message}\n")


def _http_error_status(value: str) -> int:
    try:
        status = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "HTTP status must be an integer"
        ) from exc
    if not 400 <= status <= 599:
        raise argparse.ArgumentTypeError(
            "HTTP status must be between 400 and 599"
        )
    return status


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser and its documented options."""

    parser = _Parser(
        prog="contractbreaker",
        description=(
            "Extract supported retry/timeout facts, run a deterministic "
            "runtime scenario for a supported analyzed client, and report the "
            "causal verdict. The benchmark subcommand scores labeled datasets."
        ),
        epilog=_TOP_LEVEL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="analyze one Python client source file",
        description=(
            "Analyze one supported requests client. A runnable client is "
            "executed in an isolated subprocess against an in-process test "
            "service; unsupported sources are not substituted."
        ),
        epilog=_ANALYZE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze_parser.add_argument(
        "source",
        type=Path,
        help="path to the Python client source file",
    )
    analyze_parser.add_argument(
        "--scenario",
        choices=SUPPORTED_SCENARIOS,
        default="auto",
        help=(
            "runtime scenario to run; auto only selects a runnable "
            "lost-response hypothesis"
        ),
    )
    analyze_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout format (default: text)",
    )
    analyze_parser.add_argument(
        "--output",
        type=Path,
        help="write the JSON report to this file as well",
    )
    analyze_parser.add_argument(
        "--server-idempotency",
        choices=SUPPORTED_SERVER_IDEMPOTENCY,
        default="auto",
        help=(
            "server idempotency setting; auto derives it from the client's "
            "stable-key facts"
        ),
    )
    analyze_parser.add_argument(
        "--retry-status",
        dest="retry_statuses",
        action="append",
        type=_http_error_status,
        help=(
            "retryable HTTP status for the retryable-error scenario; "
            "repeat for multiple statuses (default: 503)"
        ),
    )
    analyze_parser.add_argument(
        "--server-error-status",
        type=_http_error_status,
        help=(
            "status returned by the scenario server in the retryable-error "
            "scenario (default: 503)"
        ),
    )
    analyze_parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help=(
            "opt in to OpenAI extraction fallback for unresolved facts; "
            "requires OPENAI_API_KEY and the contractbreaker[llm] extra"
        ),
    )
    analyze_parser.add_argument(
        "--execution-timeout",
        type=float,
        default=None,
        help=(
            "maximum seconds for isolated analyzed-client execution "
            "(default: 10)"
        ),
    )
    analyze_parser.add_argument(
        "--timeout-mode",
        choices=SUPPORTED_TIMEOUT_MODES,
        default=None,
        help=(
            "ordering to simulate for the client-timeout scenario "
            "(default: client_first_then_commit)"
        ),
    )
    analyze_parser.add_argument(
        "--timeout-invariant",
        choices=tuple(TIMEOUT_INVARIANTS),
        default=None,
        help=(
            "declared contract for the client-timeout scenario "
            "(default: abort-on-timeout)"
        ),
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="run a labeled development/held-out benchmark dataset",
        description=(
            "Run every labeled benchmark case through the normal analysis "
            "pipeline. Expected labels are used only for scoring."
        ),
        epilog=_BENCHMARK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    benchmark_parser.add_argument(
        "--dataset",
        required=True,
        type=Path,
        help=(
            "dataset root containing development/manifest.json and "
            "held_out/manifest.json"
        ),
    )
    benchmark_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout format (default: text)",
    )
    benchmark_parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help=(
            "also evaluate the LLM-assisted extraction path; requires "
            "OPENAI_API_KEY and the contractbreaker[llm] extra"
        ),
    )
    benchmark_parser.add_argument(
        "--output",
        type=Path,
        help="write the benchmark JSON report to this file as well",
    )

    workflow_parser = subparsers.add_parser(
        "workflow",
        help="interpret a reliability request, plan, and optionally execute",
        description=(
            "Interpret a natural-language reliability request, analyze the "
            "source, validate readiness, and execute only with explicit "
            "approval. Plan-only mode never executes the analyzed client."
        ),
        epilog=_WORKFLOW_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    workflow_parser.add_argument(
        "--request",
        default="",
        help=(
            "natural-language reliability request to interpret; optional when "
            "--scenario and --source are both provided"
        ),
    )
    workflow_parser.add_argument(
        "--source",
        type=Path,
        help=(
            "target Python client file; may also be a .py path named in the "
            "request text"
        ),
    )
    workflow_parser.add_argument(
        "--scenario",
        choices=SUPPORTED_WORKFLOW_SCENARIOS,
        help="explicit scenario override instead of interpreting the request text",
    )
    workflow_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="produce a plan and blockers without executing the client",
    )
    workflow_parser.add_argument(
        "--plan-out",
        type=Path,
        default=None,
        help=(
            "write the plan artifact that --approve must later reference "
            "(requires --plan-only)"
        ),
    )
    workflow_parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help=(
            "plan artifact produced by --plan-only --plan-out; required with "
            "--approve and bound to the exact reviewed investigation"
        ),
    )
    workflow_parser.add_argument(
        "--allowed-root",
        type=Path,
        default=None,
        help=(
            "directory the analyzed source must live under; symlinks and '..' "
            "are resolved before the check"
        ),
    )
    workflow_parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "execute a runnable scenario; requires --plan pointing at the "
            "reviewed plan artifact"
        ),
    )
    workflow_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="stdout format (default: text)",
    )
    workflow_parser.add_argument(
        "--output",
        type=Path,
        help="write the workflow JSON document to this file as well",
    )
    workflow_parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help=(
            "opt in to the bounded OpenAI fallback for extraction and, when "
            "needed, request interpretation and explanation"
        ),
    )
    workflow_parser.add_argument(
        "--server-idempotency",
        choices=SUPPORTED_SERVER_IDEMPOTENCY,
        default=None,
        help="explicit server idempotency constraint",
    )
    workflow_parser.add_argument(
        "--retry-status",
        dest="retry_statuses",
        action="append",
        type=_http_error_status,
        help=(
            "retryable HTTP status for the retryable-error scenario; "
            "repeatable"
        ),
    )
    workflow_parser.add_argument(
        "--server-error-status",
        type=_http_error_status,
        help="status returned by the retryable-error scenario server",
    )
    workflow_parser.add_argument(
        "--execution-timeout",
        type=float,
        default=None,
        help="maximum seconds for isolated client execution (default: 10)",
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="serve the ContractBreaker developer dashboard",
        description=(
            "Serve a local developer dashboard that drives the existing "
            "ContractBreaker workflow. The browser never receives API keys "
            "and never executes analyzed code."
        ),
        epilog=_DASHBOARD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dashboard_parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="directory the dashboard may analyze Python files from (default: .)",
    )
    dashboard_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: 127.0.0.1, loopback only)",
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port to bind (default: 8765)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dashboard":
        return _run_dashboard_command(args)
    if args.command == "benchmark":
        return _run_benchmark_command(args)
    if args.command == "workflow":
        return _run_workflow_command(args)
    if args.command != "analyze":
        parser.error("a command is required")

    if args.retry_statuses is not None and args.scenario != "retryable-error":
        parser.error("--retry-status requires --scenario retryable-error")
    if args.server_error_status is not None and args.scenario != "retryable-error":
        parser.error(
            "--server-error-status requires --scenario retryable-error"
        )

    try:
        report = analyze(
            AnalysisOptions(
                source=args.source,
                scenario=args.scenario,
                server_idempotency=args.server_idempotency,
                retry_statuses=(
                    tuple(args.retry_statuses)
                    if args.retry_statuses is not None
                    else None
                ),
                server_error_status=args.server_error_status,
                execution_timeout=args.execution_timeout,
                timeout_mode=args.timeout_mode,
                timeout_invariant=args.timeout_invariant,
                llm_fallback=args.llm_fallback,
            )
        )
    except SourceReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED_INPUT
    except AnalysisUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID_ARGS

    if args.output is not None:
        try:
            write_report(report, args.output)
        except OSError as exc:
            print(
                f"error: could not write report to {args.output}: {exc}",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED_INPUT

    if args.format == "json":
        sys.stdout.write(report_to_json(report))
    else:
        print(render_text(report))

    return exit_code_for_report(report)


def _run_benchmark_command(args: argparse.Namespace) -> int:
    try:
        report = run_benchmark(
            args.dataset, llm_fallback=args.llm_fallback
        )
    except BenchmarkDatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BENCHMARK_ERROR

    if args.output is not None:
        try:
            write_benchmark_report(report, args.output)
        except OSError as exc:
            print(
                f"error: could not write benchmark report to {args.output}: {exc}",
                file=sys.stderr,
            )
            return EXIT_BENCHMARK_ERROR

    if args.format == "json":
        sys.stdout.write(benchmark_report_to_json(report))
    else:
        print(render_benchmark_text(report))

    return benchmark_exit_code(report)

def _run_dashboard_command(args: argparse.Namespace) -> int:
    if args.port < 1 or args.port > 65535:
        print("error: --port must be between 1 and 65535", file=sys.stderr)
        return EXIT_INVALID_ARGS
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: --root is not a directory: {root}", file=sys.stderr)
        return EXIT_INVALID_ARGS
    try:
        serve_dashboard(root, host=args.host, port=args.port)
    except OSError as exc:
        print(f"error: could not start dashboard server: {exc}", file=sys.stderr)
        return 2
    return 0


def _run_workflow_command(args: argparse.Namespace) -> int:
    if not args.request.strip() and not (args.scenario and args.source):
        print(
            "error: --request is required unless --scenario and --source are "
            "both provided",
            file=sys.stderr,
        )
        return EXIT_INVALID_ARGS

    flag_error = _approval_flag_error(args)
    if flag_error is not None:
        print(f"error: {flag_error}", file=sys.stderr)
        return EXIT_INVALID_ARGS

    allowed_root: Path | None = None
    if args.allowed_root is not None:
        if not args.allowed_root.is_dir():
            print(
                f"error: --allowed-root is not a directory: {args.allowed_root}",
                file=sys.stderr,
            )
            return EXIT_INVALID_ARGS
        allowed_root = args.allowed_root.resolve()

    if args.source is not None:
        violation = _containment_violation(args.source, allowed_root)
        if violation is not None:
            print(f"error: {violation}", file=sys.stderr)
            return EXIT_UNSUPPORTED_INPUT

    # The approval path and the plan-only path both need the resolved source
    # before anything executes. This first pass never runs the client.
    planning = args.approve or args.plan_only
    state = run_workflow(
        _workflow_request(
            args,
            approved=False,
            plan_only=True if planning else args.plan_only,
        )
    )

    if state.normalized is not None:
        violation = _containment_violation(state.normalized.source, allowed_root)
        if violation is not None:
            print(f"error: {violation}", file=sys.stderr)
            return EXIT_UNSUPPORTED_INPUT

    if args.plan_out is not None and state.normalized is not None:
        try:
            _write_plan_artifact(args.plan_out, state, allowed_root, args)
        except OSError as exc:
            print(
                f"error: could not write plan to {args.plan_out}: {exc}",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED_INPUT

    if not args.approve:
        return _emit_workflow_document(state, args)

    if state.normalized is None:
        return _emit_workflow_document(state, args)

    approved_digest = source_digest(state.normalized.source)
    try:
        plan = load_plan(args.plan)
        verify_plan(
            plan,
            tool_version=__version__,
            scenario=state.normalized.scenario,
            source_path=state.normalized.source.resolve(),
            source_sha256=approved_digest,
            constraints=_constraints_from_args(args),
            allowed_root=allowed_root,
        )
    except (PlanBindingError, OSError) as exc:
        print(
            "error: approval rejected: "
            f"{exc}. Re-create the plan with --plan-only --plan-out and "
            "review it again.",
            file=sys.stderr,
        )
        return EXIT_INVALID_ARGS

    # Execute exactly the bytes that were hashed and the scenario that was
    # verified: the scenario is pinned, so the execution pass can not
    # re-interpret the request into a different scenario.
    executed = run_workflow(
        _workflow_request(
            args,
            approved=True,
            plan_only=False,
            pin_scenario=state.normalized.scenario,
            expected_source_sha256=approved_digest,
        )
    )
    return _emit_workflow_document(executed, args)


def _approval_flag_error(args: argparse.Namespace) -> str | None:
    """Reject approval combinations that are not bound to a reviewed plan."""

    if args.plan_out is not None and not args.plan_only:
        return "--plan-out requires --plan-only"
    if args.plan is not None and args.plan_only:
        return "--plan cannot be combined with --plan-only"
    if args.approve and args.plan is None:
        return (
            "--approve requires --plan <file>; create one with "
            "--plan-only --plan-out <file> and review it before approving"
        )
    if args.plan is not None and not args.approve:
        return "--plan requires --approve"
    return None


def _containment_violation(
    source: Path, allowed_root: Path | None
) -> str | None:
    """Return why a source is outside the permitted root, if it is."""

    if allowed_root is None:
        return None
    try:
        resolved = Path(source).resolve()
    except OSError as exc:
        return f"source path could not be resolved: {exc}"
    if not resolved.is_relative_to(allowed_root):
        return (
            "source path is outside the allowed root: "
            f"{resolved} is not within {allowed_root}"
        )
    return None


def _constraints_from_args(args: argparse.Namespace) -> dict[str, object]:
    return normalize_constraints(
        retry_statuses=(
            tuple(args.retry_statuses)
            if args.retry_statuses is not None
            else None
        ),
        server_idempotency=args.server_idempotency,
        server_error_status=args.server_error_status,
        execution_timeout=args.execution_timeout,
        llm_fallback=args.llm_fallback,
    )


def _workflow_request(
    args: argparse.Namespace,
    *,
    approved: bool,
    plan_only: bool,
    pin_scenario: str | None = None,
    expected_source_sha256: str | None = None,
) -> WorkflowRequest:
    return WorkflowRequest(
        request=args.request,
        source=args.source,
        scenario=pin_scenario if pin_scenario is not None else args.scenario,
        retry_statuses=(
            tuple(args.retry_statuses)
            if args.retry_statuses is not None
            else None
        ),
        server_idempotency=args.server_idempotency,
        server_error_status=args.server_error_status,
        execution_timeout=args.execution_timeout,
        llm_fallback=args.llm_fallback,
        approved=approved,
        plan_only=plan_only,
        expected_source_sha256=expected_source_sha256,
    )


def _write_plan_artifact(
    path: Path,
    state: WorkflowState,
    allowed_root: Path | None,
    args: argparse.Namespace,
) -> None:
    assert state.normalized is not None
    readiness = (
        state.analysis_snapshot["hypothesis"]["scenario_readiness"]
        if state.analysis_snapshot is not None
        else {"scenario": None, "runnable": False, "blockers": ["no analysis"]}
    )
    plan = build_plan(
        tool_version=__version__,
        scenario=state.normalized.scenario,
        scenario_source=state.normalized.scenario_source,
        source_path=state.normalized.source.resolve(),
        source_sha256=source_digest(state.normalized.source),
        constraints=_constraints_from_args(args),
        allowed_root=allowed_root,
        readiness=readiness,
    )
    write_plan(path, plan)


def _emit_workflow_document(state: WorkflowState, args: argparse.Namespace) -> int:
    document = state.snapshot()
    if args.output is not None:
        try:
            args.output.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(
                f"error: could not write workflow report to {args.output}: {exc}",
                file=sys.stderr,
            )
            return EXIT_UNSUPPORTED_INPUT

    if args.format == "json":
        sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        print(render_workflow_text(state))

    return workflow_exit_code(state)
