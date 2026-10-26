# ContractBreaker

Deterministic detection of retry/timeout conflicts between an HTTP client and a
service, backed by a causal event trace rather than a pass/fail assertion.

> Status: milestone 6A. The CLI executes the analyzed client for a narrow
> supported contract and a separate benchmark command scores labeled
> development and held-out datasets. Static extraction and hypotheses never
> issue a verdict. Benchmark results are synthetic and do not establish general
> production accuracy.

## The problem

Each side can be correct in isolation and still fail together. A client retries
after a timeout; the server had already committed the first request. The result
is a duplicate side effect, such as a double charge. Types, schemas, and unit
tests can all pass while this happens.

## How it works

1. The CLI reads one supported Python client file and extracts retry, timeout,
   and idempotency facts.
2. It builds a deterministic hypothesis and checks both scenario readiness and
   the narrow executable-client contract.
3. If runnable, a subprocess worker executes the analyzed module against an
   in-process Flask test service. All `requests` traffic is routed through the
   harness adapter, socket construction is blocked, and the parent enforces an
   execution timeout.
4. The selected scenario simulates either a lost response after a server commit
   or a configured retryable HTTP error after a commit.
5. An append-only event log records requests, server receipt, commits, faults,
   replays, and the terminal outcome with explicit attempt ids.
6. The existing verifier checks the ordered causal chain and reconciles commits
   with the observable side-effect ledger.
7. `contractbreaker benchmark` runs labeled development and held-out datasets
   through the same pipeline and scores extraction, runnability, and runtime
   verification separately.

No external network or LLM calls are involved. Faults are deterministic. UUIDs
identify operations and keys, but they do not affect the verdict logic. The
bundled reference scenarios remain as programmatic verifier fixtures; the CLI
does not silently substitute them for the analyzed source.

## Strict verdict rules

A bug is `confirmed` only when the trace proves this chain:

```text
initial request (attempt N)
  -> initial side-effect commit (attempt N)
  -> dropped response (attempt N)
  -> retry request (attempt M, same operation id, M != N)
  -> second side-effect commit (attempt M)
```

Every relevant event must carry a matching attempt id, sequence numbers must be
strictly increasing, side-effect ids must be distinct, and the committed count
must equal the observed ledger count.

| Verdict | Meaning |
| --- | --- |
| `confirmed` | The complete causal chain exists and the duplicate is observable. |
| `not_reproduced` | The scenario ran, but either no retry happened or the retry was deduplicated. |
| `inconclusive` | Evidence is empty, incomplete, malformed, out of order, or contradictory. |

A passing scenario is evidence about this scenario only. It is **not** proof of
general production safety.

## CLI

After installation, the `contractbreaker` command and
`python -m contractbreaker` expose the same pipeline. The CLI analyzes **one
supported Python client file**; it does not claim to analyze arbitrary Python
repositories.

```bash
# Reproduce or clear the lost-response scenario.
contractbreaker analyze path/to/client.py --scenario lost-response

# Run the configured retryable-error policy.
contractbreaker analyze path/to/client.py --scenario retryable-error --retry-status 503

# Automatic selection (currently lost-response only).
contractbreaker analyze path/to/client.py --scenario auto --format json

# Save the JSON report while keeping human-readable stdout.
contractbreaker analyze path/to/client.py --scenario lost-response --output report.json
```

Options:

- `--format text|json` selects stdout format. JSON is written to stdout when
  selected.
- `--output PATH` always writes the JSON report to that file, regardless of
  stdout format.
- `--server-idempotency auto|on|off` controls the runtime server. `auto` derives
  the setting from the analyzed client's stable-key facts.
- `--retry-status CODE` is repeatable and only valid with
  `--scenario retryable-error`. The default is `503`, recorded with
  `cli_default` provenance.
- `--server-error-status CODE` is the status returned by the retryable-error
  scenario server. The default is `503`.
- `--scenario auto` selects `lost-response` only when that hypothesis is
  runnable. It does not guess a retryable-error policy; select that scenario
  explicitly.
- `--execution-timeout SECONDS` limits isolated analyzed-client execution. The
  default is 10 seconds; a timeout produces a structured non-runnable report.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Analysis completed with `not_reproduced`. |
| `1` | Analysis completed with `confirmed`. |
| `2` | Analysis was inconclusive or the scenario was not runnable. |
| `3` | Invalid CLI arguments or invalid argument combinations. |
| `4` | Unsupported/invalid source input or report write failure. |

Inconclusive analysis never exits `0`. `confirmed` is issued only by the
runtime causal verifier.

### Executable client contract

Runtime execution is deliberately narrower than static extraction:

- Exactly one recognized `requests` call site.
- The call must execute at module level. It may be inside module-level loops,
  conditionals, or `try` blocks, but not inside a function, class, or lambda.
- The call must use `POST` and an endpoint path of exactly `/payments`, matching
  the bundled test payment service.
- Allowed imports: `requests`, `uuid`, `json`, `typing`, `dataclasses`,
  `datetime`, `time`, `secrets`, `base64`, and `hashlib`.
- Forbidden patterns include adapters/`session.mount`, `open`, `eval`, `exec`,
  `compile`, dynamic imports, `getattr`, socket/urllib/httpx/aiohttp clients,
  and file/process modules.
- The client may be a stable-key or keyless client; conditional keys make the
  hypothesis ambiguous and non-runnable.

The worker executes the module in a separate Python process with a temporary
working directory. It routes every `requests` call to an in-process WSGI service
and replaces `socket.socket`/`socket.create_connection` with blocking guards. It
assigns `X-Operation-Id` only when the client does not supply one. Source output
is suppressed. If the source hangs, the parent terminates the worker at the
configured timeout.

This is isolation for deterministic testing, not a hardened security sandbox.
The source still runs with the invoking user's privileges. Unsupported or
unreliable sources are reported as non-runnable; they are never silently
replaced by the reference client.

### Report schema

Reports use `schema_version = "1.0"` and contain:

| Field | Meaning |
| --- | --- |
| `schema_version`, `tool_version` | Report/package versions. |
| `analysis_status` | `executed`, `not_runnable`, or `unsupported_input`. |
| `scenario` | Requested scenario. |
| `selected_scenario` | Scenario actually selected, or `null`. |
| `source_file` | Analyzed file path. |
| `extraction` | AST facts, locations, statuses, warnings; provenance `static_extraction`. |
| `hypothesis` | Derived facts, unknowns, blockers, warnings, and scenario readiness; provenance `derived_from_extraction`. |
| `scenario_configuration` | Effective settings, runner, execution state, and per-setting provenance. |
| `execution` | Runtime event trace, attempt/operation/side-effect IDs, and observed count; provenance `runtime_observation`. |
| `verdict`, `verdict_reason` | Final runtime verdict and evidence reason. |
| `verdict_provenance` | `runtime_verifier` or `not_executed`. |
| `limitations` | Scope and simulation caveats. |

Provenance values distinguish static facts from other origins:
`static_extraction`, `derived_from_extraction`, `cli`, `cli_default`,
`scenario_default`, `runtime_observation`, `runtime_verifier`, and
`not_executed`.

An abridged confirmed report (the full event list is produced by the CLI):

```json
{
  "schema_version": "1.0",
  "tool_version": "0.1.0",
  "analysis_status": "executed",
  "scenario": "lost-response",
  "selected_scenario": "lost-response",
  "source_file": "client.py",
  "extraction": {
    "status": "supported",
    "provenance": "static_extraction",
    "warnings": [],
    "calls": [
      {
        "location": {"file": "client.py", "line": 3, "column": 9, "end_line": 9, "end_column": 10},
        "method": {"status": "known", "value": "POST", "reason": null},
        "endpoint": {"status": "known", "value": "http://service.test/payments", "reason": null},
        "timeout": {"status": "known", "value": 5, "reason": null},
        "idempotency_key": {"status": "absent", "value": null, "reason": "no Idempotency-Key header found"}
      }
    ],
    "retry_loops": [
      {
        "location": {"file": "client.py", "line": 2, "column": 1, "end_line": 10, "end_column": 20},
        "max_attempts": {"status": "known", "value": 2, "reason": "range(2) iterations"},
        "retry_count": {"status": "known", "value": 1, "reason": "derived as max_attempts - 1"},
        "triggers": {"status": "known", "value": ["requests.exceptions.Timeout"], "reason": null}
      }
    ]
  },
  "hypothesis": {
    "provenance": "derived_from_extraction",
    "unknowns": [],
    "warnings": [],
    "idempotency_key_present": {"status": "absent", "value": null, "reason": "no Idempotency-Key header found"},
    "scenario_readiness": {"scenario": "lost-response", "runnable": true, "blockers": [], "unknowns": []}
  },
  "scenario_configuration": {
    "executed": true,
    "scenario": "lost-response",
    "runner": "runtime_subprocess",
    "settings": {
      "entrypoint": "module",
      "server_supports_idempotency": false,
      "client_sends_idempotency_key": false,
      "max_retries": 1,
      "drop_response_on_attempts": [1],
      "request_timeout": 5.0,
      "execution_timeout_seconds": 10.0,
      "operation_id_source": "harness_if_absent"
    },
    "provenance": {
      "entrypoint": "scenario_default",
      "server_supports_idempotency": "derived_from_extraction",
      "client_sends_idempotency_key": "derived_from_extraction",
      "max_retries": "derived_from_extraction",
      "drop_response_on_attempts": "scenario_default",
      "request_timeout": "derived_from_extraction",
      "execution_timeout_seconds": "scenario_default",
      "operation_id_source": "scenario_default"
    },
    "reason": null
  },
  "execution": {
    "occurred": true,
    "provenance": "runtime_observation",
    "runner": "runtime_subprocess",
    "events": [
      {"seq": 1, "kind": "request_sent", "attempt_id": "attempt-1", "operation_id": "op-demo", "data": {"method": "POST", "path": "/payments"}},
      {"seq": 2, "kind": "side_effect_committed", "attempt_id": "attempt-1", "operation_id": "op-demo", "data": {"side_effect_id": "effect-1"}},
      {"seq": 3, "kind": "response_dropped", "attempt_id": "attempt-1", "operation_id": "op-demo", "data": {"status_code": 201}},
      {"seq": 4, "kind": "request_sent", "attempt_id": "attempt-2", "operation_id": "op-demo", "data": {"method": "POST", "path": "/payments"}},
      {"seq": 5, "kind": "side_effect_committed", "attempt_id": "attempt-2", "operation_id": "op-demo", "data": {"side_effect_id": "effect-2"}}
    ],
    "attempt_ids": ["attempt-1", "attempt-2"],
    "operation_ids": ["op-demo"],
    "side_effect_ids": ["effect-1", "effect-2"],
    "observed_side_effects": 2
  },
  "verdict": "confirmed",
  "verdict_reason": "retry after a lost response committed a second side effect (2 side effects for one logical operation)",
  "verdict_provenance": "runtime_verifier",
  "limitations": [
    "The analyzed client is executed in a subprocess against an in-process WSGI test service; this is isolation, not a hardened security sandbox."
  ]
}
```

JSON output is sorted deterministically and contains only JSON-native values.

## AST extraction

`contractbreaker.extract_from_source` and `extract_from_file` return typed,
JSON-serializable facts with source locations. The extractor uses the Python
standard library only and does not call an LLM.

```python
from contractbreaker import extract_from_source

result = extract_from_source(source_text, filename="client.py")
print(result.to_json(indent=2))
```

Each fact carries an explicit status:

| Fact status | Meaning |
| --- | --- |
| `known` | A value or behavior was determined statically. |
| `absent` | The pattern is confirmed not to be present. |
| `unknown` | The source does not determine the value. |
| `ambiguous` | Multiple readings are possible. |
| `unsupported` | The pattern is outside this extractor's scope. |

Missing values are never replaced with defaults. For example, a call with no
`timeout=` argument reports `timeout.status = "absent"`, while a timeout passed
through a variable reports `"unknown"`, and an explicit `timeout=None` reports
`"known"` with value `None`.

### Supported patterns

- `requests.get/post/put/patch/delete/head/options` and module/function aliases.
- `requests.request("METHOD", ...)` with a literal method.
- Tracked session objects created from `requests.Session()` and used as
  `session.get(...)`, including `with requests.Session() as session`.
- Injected sessions with an explicit annotation, such as
  `def __init__(self, session: requests.Session): self._session = session`,
  followed by `self._session.post(...)`.
- Conditional header mutations are retained but mark an `Idempotency-Key` as
  `ambiguous`, because its presence depends on runtime state.
- Literal numeric or `None` timeout values.
- `for ... in range(...)` retry loops with literal or simply assigned integer
  bounds. `max_attempts` and the derived `retry_count` are separate facts.
- `try/except` handlers inside a retry loop for requests `Timeout`,
  `ReadTimeout`, `ConnectTimeout`, and `ConnectionError`, including imported
  aliases and tuples.
- Literal header dictionaries, simple `headers = {...}` variables, literal
  subscript updates, and `headers.update({...})`.
- `Idempotency-Key` presence and whether a generated key is evaluated inside or
  outside the retry loop.

### Explicitly unsupported

The extractor reports these as warnings and does not claim to understand them:

- `urllib3.Retry`, `HTTPAdapter(max_retries=...)`, and dynamic retry policies.
- `tenacity`, `backoff`, and retry decorators.
- `while`-loop retries and non-range retry loops.
- HTTP-like calls whose receiver is not a tracked requests module or session,
  such as wrapper clients or unsupported HTTP libraries.
- Tuple/list timeouts.
- Cross-file or interprocedural wrapper analysis.

A recognized file is `supported` only when no extracted fact is unknown,
ambiguous, or unsupported and no warning was raised. Files with recognized
calls but unresolved details are `partial`; unrecognized HTTP libraries are
`unsupported`; valid files with no recognized requests calls are
`no_http_calls`.

## Hypothesis generation

`hypothesize_from_source`, `hypothesize_from_file`, and
`hypothesize_from_extraction` convert extraction facts into a typed
`ClientHypothesis`. The hypothesis records the HTTP method, endpoint, timeout,
retry count, retry triggers, idempotency-key presence and reuse, all unresolved
facts, and warnings.

```python
from contractbreaker import hypothesize_from_source, recommend_scenario

hypothesis = hypothesize_from_source(source_text, filename="client.py")
if hypothesis.scenario_ready:
    recommendation = recommend_scenario(hypothesis)
```

A hypothesis is runnable only when the method, endpoint, positive timeout,
retry count, timeout/connection trigger, and idempotency-key behavior are all
resolved. Otherwise `scenario_ready` is false, `blockers` explains why, and
`recommend_scenario` raises `HypothesisNotRunnable`. Static extraction has no
verdict field; only the causal verifier can return `confirmed`.

`examples/payments/scenario.py` still provides `config_from_hypothesis` for
programmatic reference-scenario tests. The CLI no longer uses that mapping: for
a runnable hypothesis it executes the analyzed client source itself.

The reference payment client is a deliberate partial example: its injected
session is recognized, but its f-string endpoint, `self._timeout`, and
`self._max_retries` are not literal; its key is conditional; and its HTTP call
is inside a method rather than at module level. The hypothesis reports those
facts and refuses runtime execution until the source satisfies the executable
client contract.

## Lost-response payment scenario

`examples/payments` contains the reference payment fixtures:

- The reference client retries timeouts and optionally sends a stable
  `Idempotency-Key` plus a stable `X-Operation-Id`.
- The service optionally deduplicates known idempotency keys and appends every
  committed payment to a ledger.
- `examples/payments/scenario.py` wires both sides to the fault adapter and
  returns a verifier `Report` for programmatic tests.

The CLI does not use the reference client. It executes the analyzed module
against the same test service, with the harness dropping the first response
after the server commits.

## Retryable-error scenario

This is a separate scenario from the lost-response case. The server commits the
side effect first and then returns an HTTP error such as `503`. A client retries
only because an explicit `RetryPolicy` includes that status.

For CLI runtime execution, the worker returns the configured error to the
analyzed client and records a retry decision only when a subsequent request
follows that configured status. The policy is never inferred merely from the
presence of a 503 response.

`examples/payments/retryable_error_scenario.py` remains the programmatic
reference scenario and returns a verifier `Report`. Its trace contains:

- `retry_policy_configured` with the exact retryable status set and retry budget.
- `request_sent` and `request_received` per attempt.
- `side_effect_committed` with a unique side-effect id.
- `retryable_error_returned` tied to the attempt that received the error.
- `retry_scheduled` tied to the same attempt and recording the status that
  triggered the retry.
- A retry request with a new attempt id and the same operation id.

The verifier confirms a duplicate only when the first commit precedes the
retryable error, the retry decision follows that error and matches the configured
policy, the retry request follows the decision, and the retry commits a second
unique side effect. A retry request without a recorded policy decision is
`inconclusive`; a 5xx status is never assumed to be retried.

The fixed variant enables server idempotency and sends a stable key. The retry
receives an `idempotent_replay` and only one side effect exists, producing
`not_reproduced`.

## Setup

Requires Python 3.10+.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python demo.py
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python demo.py
```

The test suite is deterministic and does not require network access. The
installation exposes both `contractbreaker` and `python -m contractbreaker`.
The editable install also packages the bundled test services so the runtime
worker works outside the repository root.

## Tests

The complete suite currently contains 106 tests.

`tests/test_duplicate_side_effects.py` covers the strict causal verifier:

- Buggy client and server: `confirmed`, two side effects.
- Idempotency protection on both sides: `not_reproduced`, one side effect.
- Client that never retries: `not_reproduced`, one side effect.
- Server supports idempotency but the client sends no key: still `confirmed`.
- Incomplete trace: `inconclusive`.
- Incorrect event ordering: not `confirmed`.
- Mismatched attempt identifiers: not `confirmed`.
- Malformed or contradictory evidence: `inconclusive`.

`tests/test_ast_extractor.py` contains 33 labeled extraction cases covering:

- Supported calls, aliases, sessions, multiline calls, and source locations.
- Literal, missing, dynamic, explicit-`None`, and tuple timeouts.
- Retry loops with different literal and named bounds.
- Timeout/connection exception handlers and aliases.
- Idempotency keys generated inside versus outside retry loops.
- Missing or unknown headers and session-level header ambiguity.
- Injected annotated sessions and conditional idempotency headers.
- Unsupported tenacity, urllib3/HTTPAdapter, while-loop, and wrapper patterns.
- JSON round-tripping, parse errors, and files without HTTP calls.

`tests/test_hypothesis_integration.py` adds 6 end-to-end cases:

- Buggy source -> hypothesis -> reference scenario -> `confirmed`.
- Protected source with stable key -> hypothesis -> scenario ->
  `not_reproduced`.
- Reference client recognized as a call but not runnable.
- Unknown retry bound blocks scenario construction.
- Unsupported extraction blocks scenario construction.
- Per-attempt idempotency key is not mapped onto the stable-key scenario.

`tests/test_retryable_error_scenario.py` adds 12 cases:

- Buggy retryable error: `confirmed`, two side effects.
- Fixed idempotency variant: `not_reproduced`, one side effect.
- Empty policy, status outside the policy, and zero retry budget: no retry.
- Missing retryable-error event, missing policy event, and retry without a
  recorded policy decision: `inconclusive`.
- Retry decision without a retry request, incorrect ordering, mismatched
  attempt ids, and policy mismatch: `inconclusive`.

`tests/test_cli.py` adds 20 CLI/report cases:

- Buggy and protected clients for both runtime scenarios.
- Automatic lost-response selection.
- Unknown retry limit and unknown timeout produce inconclusive exit `2`.
- Unsupported clients and ambiguous idempotency keys never become confirmed.
- Invalid scenarios and invalid argument combinations exit `3`.
- Missing files and output-write failures exit `4`.
- JSON schema stability, JSON-native serialization, output-file writing, and
  invocation through `python -m contractbreaker` from outside the repository.

`tests/test_runtime_contract.py` adds 7 contract cases:

- Module-level requests clients are supported.
- Nested calls, disallowed imports, requests adapter imports, forbidden calls,
  `session.mount`, and files without a recognized call are non-runnable.

`tests/test_runtime_execution.py` adds 8 runtime cases:

- The actual buggy lost-response client confirms and the fixed client does not.
- The actual buggy retryable-error client confirms and the fixed client does not.
- A client that does not retry is not falsely confirmed.
- A nested client is not executed or substituted.
- An incomplete runtime trace is inconclusive.
- Execution timeout maps to a structured failure.

`tests/test_benchmark.py` adds 8 benchmark cases:

- Known metric arithmetic and denominators.
- Development/held-out CLI JSON, output files, and unsupported non-execution.
- Labels cannot change the runtime verdict.
- Per-case analysis errors are recorded, not omitted.
- Missing/invalid datasets fail with exit `2`.
- Missing `--dataset` is invalid CLI usage.
- Dataset sources cannot escape their split directory.

## Benchmark (milestone 6A)

The benchmark is a small **synthetic** dataset, not evidence of general
production accuracy. It contains a development split and a separately stored
held-out split.

### Dataset structure

```text
benchmark/
  development/
    manifest.json
    dev_*.py
  held_out/
    manifest.json
    held_*.py
```

Dataset name: `contractbreaker-synthetic`; dataset version: `1.0`.

Each manifest case labels:

- Inputs passed to the normal pipeline: scenario, server idempotency, retry
  statuses, server error status, and execution timeout.
- Expected extraction status and labeled facts.
- Expected warning codes.
- Expected runnability, whether execution should occur, verdict, and
  side-effect count.

Expected labels are **not** passed to `analyze`. The benchmark passes only the
case inputs to the runtime pipeline, then scores the resulting report against
the labels. This is tested by deliberately mislabeling a held-out case: the
runtime still returns `confirmed`, and the benchmark records a mismatch.

### Development versus held-out

Development cases cover patterns used while building the extractor/runtime:
explicit `for` retries, literal and unresolved timeouts, stable uuid keys,
fresh keys, conditional headers, no-retry exception handling, 503 status
checks, nested wrappers, and an unsupported `httpx` client.

Held-out cases were authored after implementation and include patterns not used
to tune the detector: `while` retries, a retry wrapper, a three-attempt loop
with a `secrets` key, `>=500` status checks, a policy mismatch, a fresh key,
a zero timeout, tenacity, and a parse error.

During initial label verification, status-check cases were correctly classified
as `extraction_status: partial` because retry triggers are unknown. One
one-iteration no-retry case was clarified as verifier-`inconclusive` and moved
from held-out to development. No extractor, runtime, report, or verifier logic
was changed in response. This is a threat to validity and is disclosed rather
than hidden.

No independently sourced public client examples are included. The supported
runtime contract is narrow, and a public example would need licensing,
dependency, and version review. All benchmark results are therefore labeled
synthetic.

### Commands

```bash
# Human-readable summary.
contractbreaker benchmark --dataset benchmark --format text

# Machine-readable report, including labels and per-case outcomes.
contractbreaker benchmark --dataset benchmark --format json

# Write the JSON benchmark report to a file.
contractbreaker benchmark --dataset benchmark --format json \
  --output benchmark-report.json
```

Benchmark exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Every labeled case matched. |
| `1` | Completed with one or more mismatches. |
| `2` | Dataset or execution could not be completed. |
| `3` | Invalid CLI arguments. |

### Metric definitions

All rates include an explicit numerator and denominator in JSON.

- **Extraction status accuracy:** cases whose actual extraction status matches
  the expected status, over all cases.
- **Fact accuracy:** per labeled fact, matching status and value for `known`
  facts or matching status for `absent`/`unknown`/`ambiguous`/`unsupported`.
  Denominator: cases where that fact is labeled.
- **Warning code accuracy:** cases whose extraction warning-code set matches
  exactly, over all cases.
- **Uncertainty accuracy:** expected unsupported extraction statuses plus fact
  labels with status `unknown`, `ambiguous`, or `unsupported`, over that set.
- **Unsupported input accuracy:** cases expected as `unsupported`,
  `no_http_calls`, or `parse_error` whose extraction status matches.
- **Runnability:** counts of correctly runnable, correctly blocked, incorrectly
  allowed, and incorrectly blocked cases; accuracy is correct cases over all.
- **Detection rate:** expected supported buggy cases (`runnable` and expected
  `confirmed`) that actually return `confirmed`.
- **False-positive rate:** expected supported fixed cases (`runnable` and
  expected `not_reproduced`) that actually return `confirmed`.
- **Side-effect accuracy:** cases with a non-null expected side-effect count
  whose actual count matches.
- **Inconclusive cases:** all cases whose actual verdict is `inconclusive`.
- **Verdict accuracy:** expected verdict matched over all cases.
- **Executed verdict accuracy:** expected verdict matched over cases that
  actually executed; this measures verifier outcomes on real traces.
- **Execution failures:** cases expected to execute that did not.
- **Unexpected executions:** cases expected not to execute that did.
- **Case errors:** cases where the analysis raised and the benchmark recorded an
  error instead of omitting the case.

### Current synthetic results

All 13 development and 10 held-out labeled cases matched their expected
labels.

| Split | Cases | Status | Facts | Warnings | Uncertainty | Runnable/blocked | Incorrectly allowed | Detection | False positives | Side effects | Inconclusive |
| --- | ---: | --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: |
| Development | 13 | 13/13 | 12/12 each | 13/13 | 8/8 | 7/6 | 0 | 2/2 | 0/4 | 7/7 | 7 |
| Held-out | 10 | 10/10 | 9/9 each | 10/10 | 4/4 | 4/6 | 0 | 1/1 | 0/3 | 4/4 | 6 |

Held-out inconclusive cases: `held-lost-while-buggy`,
`held-lost-retry-wrapper`, `held-retry-fresh-key`, `held-timeout-zero`,
`held-unsupported-tenacity`, `held-parse-error`.

These numbers demonstrate the benchmark pipeline and the current narrow
contract. They do not establish broad extraction accuracy, production safety,
or generalization beyond this synthetic dataset.

## Known limitations

- **Simulated failures only.** The analyzed client really executes, but its
  `requests` traffic is routed into a Flask test client. The adapter raises
  `ReadTimeout` or returns a configured `503` after in-process server execution.
  It does not inject real network-level failures, packet loss, or a real dropped
  HTTP response.
- **Two runtime scenarios only.** Lost-response and commit-then-retryable-error
  are covered. No other failure modes or retry backoff schedules are modeled.
- **Executable support is narrow.** Runtime execution requires one module-level
  `requests` call site, `POST /payments`, and allowlisted imports. Calls inside
  functions/classes/lambdas, custom adapters, `open`, dynamic imports, and
  non-requests HTTP clients are non-runnable.
- **Isolation is not a security sandbox.** The analyzed source runs in a
  subprocess with the invoking user's privileges. The harness blocks socket
  construction and routes requests, but it does not provide OS-level sandboxing.
  Execution is capped by `--execution-timeout` (default 10 seconds).
- **Narrow AST support.** The extractor handles only explicit requests patterns
  listed above. It does not perform data-flow analysis, trace wrappers across
  files, evaluate arbitrary expressions, follow decorators, or understand
  third-party retry policies. Injected sessions require an explicit
  `requests.Session` annotation.
- **Static assumptions.** Retry bounds are interpreted as one HTTP attempt per
  loop iteration; requests before the loop are not counted. Generated key
  stability is reported unknown unless a known fresh-value generator is
  evaluated inside the loop. Static retry counts and timeouts are expectations
  recorded for context; the analyzed client's code controls actual runtime
  attempts and timeouts.
- **Reference client is not runtime-runnable.** Its call is recognized through
  the annotated injected session, but the endpoint, timeout, and retry limit are
  instance attributes, the key is conditional, and the call is inside a method
  rather than at module level. The hypothesis correctly refuses execution.
- **Hypothesis scope.** A runnable lost-response hypothesis requires exactly one
  recognized call, a literal positive timeout, a known retry count, an identified
  timeout/connection trigger, and a stable or absent idempotency key.
  `--scenario auto` currently selects only the lost-response scenario.
- **Retryable-error scenario is configured explicitly.** Its status policy comes
  from CLI flags/defaults, not static extraction. Its verdict depends on the
  configured status set and retry budget; another policy must be modeled
  separately.
- **The CLI analyzes one supplied file.** It does not perform arbitrary
  repository analysis, multi-file data flow, wrapper resolution, or package
  discovery.
- **Benchmark results are synthetic and small.** The development and held-out
  splits were authored by the same project, no independent public clients are
  included, and the denominators are small (13 and 10 cases). A 100% synthetic
  result is not a generalization claim. Label corrections made during the
  initial benchmark run are disclosed in the benchmark section.
- **Unsupported cases are excluded from detection denominators.** They count
  toward extraction/blocking metrics and inconclusive counts, but are never
  counted as successful bug detections.
- **Benchmark labels are scoring-only.** The benchmark passes only case inputs
  to the runtime pipeline; a dedicated test mislabels a case and confirms that
  the runtime verdict does not change.
- **Reports are scenario evidence, not a security audit.** Unsupported patterns,
  unknown facts, and non-runnable hypotheses are reported as such and can never
  become `confirmed`.
- **Not a safety proof.** A `not_reproduced` verdict says this deterministic
  scenario did not produce the duplicate; it does not prove the guard is
  correct for every payload, key scope, or deployment topology.
- **Not implemented yet:** LLM fallback, dashboard, and Alan integration.