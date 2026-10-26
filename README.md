# ContractBreaker

Deterministic detection of retry/timeout conflicts between an HTTP client and a
service, backed by a causal event trace rather than a pass/fail assertion.

> Status: milestone 4. The build verifies two distinct failure scenarios and
> exposes the full extraction -> hypothesis -> scenario -> verifier pipeline
> through a developer CLI with human-readable and JSON reports. Static
> extraction never issues a verdict.

## The problem

Each side can be correct in isolation and still fail together. A client retries
after a timeout; the server had already committed the first request. The result
is a duplicate side effect, such as a double charge. Types, schemas, and unit
tests can all pass while this happens.

## How it works

1. The real reference client talks to the real reference server in-process.
2. `FaultInjectionAdapter` drops the response on a selected attempt **after**
   the server has finished handling it, so the side effect is committed but the
   client observes a timeout.
3. The client retries the same logical operation when allowed.
4. An append-only event log records requests, server receipt, commits, dropped
   responses, replays, and the terminal outcome with explicit attempt ids.
5. The verifier checks the ordered causal chain and reconciles commits with the
   observable side-effect ledger.

No sockets, sleeps, or LLM calls are involved. UUIDs identify operations and
keys, but they do not affect the verdict logic.

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
    "runner": "run_payment_scenario",
    "settings": {
      "server_supports_idempotency": false,
      "client_sends_idempotency_key": false,
      "max_retries": 1,
      "drop_response_on_attempts": [1],
      "request_timeout": 5.0
    },
    "provenance": {
      "server_supports_idempotency": "derived_from_extraction",
      "client_sends_idempotency_key": "derived_from_extraction",
      "max_retries": "derived_from_extraction",
      "drop_response_on_attempts": "scenario_default",
      "request_timeout": "derived_from_extraction"
    },
    "reason": null
  },
  "execution": {
    "occurred": true,
    "provenance": "runtime_observation",
    "runner": "run_payment_scenario",
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
    "Runtime scenarios execute the bundled reference harness, not the analyzed source file."
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

`examples/payments/scenario.py` provides `config_from_hypothesis`, which maps a
runnable hypothesis onto the existing reference scenario. The end-to-end tests
extract source, build a hypothesis, construct a scenario config, run the
scenario, and assert the runtime verdict.

The reference payment client is a deliberate partial example: its injected
session is now recognized, but its f-string endpoint, `self._timeout`, and
`self._max_retries` are not literal, and its key is conditional. The hypothesis
therefore reports the call but refuses to run until those facts are made
statically explicit.

## Lost-response payment scenario

`examples/payments` contains a small payment client and Flask service:

- The client retries timeouts and optionally sends a stable
  `Idempotency-Key` plus a stable `X-Operation-Id`.
- The service optionally deduplicates known idempotency keys and appends every
  committed payment to a ledger.
- `examples/payments/scenario.py` wires both sides to the fault adapter and
  returns a verifier `Report`.

## Retryable-error scenario

This is a separate scenario from the lost-response case. The server commits the
side effect first and then returns an HTTP error such as `503`. A client retries
only because an explicit `RetryPolicy` includes that status.

`examples/payments/retryable_error_scenario.py` runs the scenario and returns a
verifier `Report`. The trace contains:

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
The editable install also packages the bundled reference scenarios so the CLI
works outside the repository root.

## Tests

The complete suite currently contains 82 tests.

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

`tests/test_cli.py` adds 19 CLI/report cases:

- Buggy and protected clients for both runtime scenarios.
- Automatic lost-response selection.
- Unknown retry limit and unknown timeout produce inconclusive exit `2`.
- Unsupported clients and ambiguous idempotency keys never become confirmed.
- Invalid scenarios and invalid argument combinations exit `3`.
- Missing files and output-write failures exit `4`.
- JSON schema stability, JSON-native serialization, output-file writing, and
  invocation through `python -m contractbreaker` from outside the repository.

## Known limitations

- **Simulated failures only.** The adapter routes `requests` calls into a Flask
  test client and raises a `ReadTimeout` after in-process server execution. It
  does not inject real network-level failures, packet loss, or a real dropped
  HTTP response. The boundary being modeled is "server committed, client saw a
  timeout", not an actual socket-level fault.
- **Two runtime scenarios only.** Lost-response and commit-then-retryable-error
  are covered. No other failure modes or retry backoff schedules are modeled.
- **Narrow AST support.** The extractor handles only explicit requests patterns
  listed above. It does not perform data-flow analysis, trace wrappers across
  files, evaluate arbitrary expressions, follow decorators, or understand
  third-party retry policies. Injected sessions require an explicit
  `requests.Session` annotation.
- **Static assumptions.** Retry bounds are interpreted as one HTTP attempt per
  loop iteration; requests before the loop are not counted. Generated key
  stability is reported unknown unless a known fresh-value generator is
  evaluated inside the loop.
- **Reference client is not runnable.** Its call is recognized through the
  annotated injected session, but the endpoint, timeout, and retry limit are
  instance attributes and the key is conditional. Those facts stay unknown or
  ambiguous, so the hypothesis correctly refuses to configure a scenario.
- **Hypothesis scope.** A runnable lost-response hypothesis requires exactly one
  recognized call, a literal positive timeout, a known retry count, an identified
  timeout/connection trigger, and a stable or absent idempotency key.
  `--scenario auto` currently selects only the lost-response scenario.
- **Retryable-error scenario is configured explicitly.** Its status policy comes
  from CLI flags/defaults, not static extraction. Its verdict depends on the
  configured status set and retry budget; another policy must be modeled
  separately.
- **The CLI does not execute the analyzed source file.** It derives a
  hypothesis and configures the bundled reference harness. Runtime files,
  site-packages, multiple source files, and arbitrary repository layouts are not
  analyzed.
- **Reports are scenario evidence, not a security audit.** Unsupported patterns,
  unknown facts, and non-runnable hypotheses are reported as such and can never
  become `confirmed`.
- **Not a safety proof.** A `not_reproduced` verdict says this deterministic
  scenario did not produce the duplicate; it does not prove the guard is
  correct for every payload, key scope, or deployment topology.
- **Not implemented yet:** LLM fallback, dashboard, and Alan integration.