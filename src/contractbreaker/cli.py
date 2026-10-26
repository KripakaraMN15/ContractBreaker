"""Developer CLI for the deterministic ContractBreaker pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .pipeline import (
    SUPPORTED_SCENARIOS,
    SUPPORTED_SERVER_IDEMPOTENCY,
    AnalysisOptions,
    AnalysisUsageError,
    SourceReadError,
    analyze,
)
from .report import (
    EXIT_INVALID_ARGS,
    EXIT_UNSUPPORTED_INPUT,
    exit_code_for_report,
    render_text,
    report_to_json,
    write_report,
)
from .version import __version__

_EXIT_EPILOG = """exit codes:
  0  analysis completed: not_reproduced
  1  analysis completed: confirmed
  2  analysis completed: inconclusive or not runnable
  3  invalid CLI arguments
  4  unsupported/invalid input or report write failure
"""


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
            "Extract supported retry/timeout facts, derive a deterministic "
            "hypothesis, run one reference scenario, and report the causal verdict."
        ),
        epilog=_EXIT_EPILOG,
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
        epilog=_EXIT_EPILOG,
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
        "--execution-timeout",
        type=float,
        default=None,
        help=(
            "maximum seconds for isolated analyzed-client execution "
            "(default: 10)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)

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