# ContractBreaker

Deterministic detection of retry/timeout conflicts between an HTTP client and a
service, backed by a causal event trace rather than a pass/fail assertion.

> Status: milestone 6B. Deterministic extraction, runtime execution, and
> benchmark scoring remain the baseline. An opt-in OpenAI fallback may propose
> unresolved facts, but every accepted fact requires validated source evidence
> and the existing runtime gates and verifier remain authoritative. Benchmark
> results are synthetic and do not establish general production accuracy.

## The problem

Each side can be correct in isolation and still fail together. A client retries
after a timeout; the server had already committed the first request. The result
is a duplicate side effect, such as a double charge. Types, schemas, and unit
tests can all pass while this happens.

## How it works

1. The CLI reads one supported Python client file and extracts retry, timeout,
   and idempotency facts.
2. It builds a deterministic hypothesis. If `--llm-fallback` is enabled,
   unresolved facts may be proposed by OpenAI, validated against exact source
   evidence, and conservatively merged before the hypothesis is rebuilt.
3. It checks both scenario readiness and the narrow executable-client contract.
4. If runnable, a subprocess worker executes the analyzed module against an
   in-process Flask test service. All `requests` traffic is routed through the
   harness adapter, socket construction is blocked, and the parent enforces an
   execution timeout.
5. The selected scenario simulates either a lost response after a server commit
   or a configured retryable HTTP error after a commit.
6. An append-only event log records requests, server receipt, commits, faults,
   replays, and the terminal outcome with explicit attempt ids.
7. The existing verifier checks the ordered causal chain and reconciles commits
   with the observable side-effect ledger.
8. `contractbreaker benchmark` runs labeled development and held-out datasets
   through the same pipeline and scores extraction, runnability, and runtime
   verification separately, optionally with LLM assistance.

Deterministic analysis makes no external network or LLM calls. The opt-in LLM
fallback makes an OpenAI request only when explicitly enabled and unresolved
facts exist. Faults are deterministic. UUIDs identify operations and keys, but
they do not affect the verdict logic. The bundled reference scenarios remain as
programmatic verifier fixtures; the CLI does not silently substitute them for
the analyzed source.

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

# Opt in to the OpenAI extraction fallback for unresolved facts.
contractbreaker analyze path/to/client.py --scenario lost-response --llm-fallback
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
- `--llm-fallback` opts in to the OpenAI fallback for unresolved extraction
  facts. It may make a paid API request. See the LLM fallback section below.

## Workflow (milestone 7)

`contractbreaker workflow` adds an evidence-driven orchestration layer on top of
the existing pipeline. It interprets a request, analyzes the source, validates
readiness, requires approval, executes only if allowed, and explains the
evidence.

```bash
# Inspect a request and produce a plan. Never executes the client.
contractbreaker workflow \
  --request "check for duplicate payments after a lost response" \
  --source path/to/client.py \
  --plan-only

# Plan with JSON output (no execution).
contractbreaker workflow --request "retry on a 503 server error" \
  --source path/to/client.py --plan-only --format json

# Execute only after explicit approval.
contractbreaker workflow --request "check a lost response" \
  --source path/to/client.py --approve

# Structured input without free text, executed with approval.
contractbreaker workflow --scenario lost-response \
  --source path/to/client.py --approve
```

Workflow stages, in order:

1. `request_interpretation` — resolves scenario, source, and explicit
   constraints. Deterministic keyword mapping is preferred. Both scenarios
   mentioned → blocked as ambiguous. No scenario → blocked as underspecified
   unless an optional LLM proposes one with a phrase copied verbatim from the
   request. Unsupported topics (circuit breakers, rate limits, DNS, gRPC,
   WebSockets, databases, connection resets) are rejected without guessing.
2. `source_analysis` — deterministic extractor, then the optional bounded LLM
   fallback, with provenance, evidence validation, and conflicts preserved.
3. `readiness_validation` — existing hypothesis and runtime-contract checks.
   Blockers stop the workflow before execution.
4. `approval` — execution requires `--approve`; `--plan-only` always stops.
5. `execution` — the existing subprocess runtime and scenario verifier.
6. `explanation` — deterministic evidence summary, optionally replaced by an
   LLM summary that is validated against the report verdict and observed
   side-effect count. Invalid summaries are discarded and the deterministic
   summary is used.

The workflow JSON document adds `workflow_schema_version`, `outcome`,
`request`, `approval`, `stages`, `analysis_snapshot`, `analysis` (the existing
versioned report when executed), `verdict`, `explanation`, `explanation_meta`,
`limitations`, and `errors`. The existing analysis report contract is unchanged.

Workflow exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Executed; runtime verdict `not_reproduced`. |
| `1` | Executed; runtime verdict `confirmed`. |
| `2` | Planned, approval required, not runnable, or inconclusive. |
| `3` | Invalid or underspecified request. |
| `4` | Unsupported request or input, including unsupported source. |

A plan-only run and an unapproved run both exit `2`, never `0`. Only the
runtime verifier assigns the workflow verdict; the orchestrator and the
optional summarizer cannot change it.

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
| `hypothesis` | Derived facts, unknowns, blockers, warnings, scenario readiness, optional `fact_provenance`, and optional `llm_fallback` diagnostics; provenance `derived_from_extraction`. |
| `scenario_configuration` | Effective settings, runner, execution state, and per-setting provenance. |
| `execution` | Runtime event trace, attempt/operation/side-effect IDs, and observed count; provenance `runtime_observation`. |
| `verdict`, `verdict_reason` | Final runtime verdict and evidence reason. |
| `verdict_provenance` | `runtime_verifier` or `not_executed`. |
| `limitations` | Scope and simulation caveats. |

Provenance values distinguish static facts from other origins:
`static_extraction`, `derived_from_extraction`, `llm_assisted`, `cli`,
`cli_default`, `scenario_default`, `runtime_observation`,
`runtime_verifier`, and `not_executed`.

Milestone 6B keeps the top-level report keys and `schema_version` at `"1.0"`.
It adds `hypothesis.fact_provenance` and `hypothesis.llm_fallback` as additive
fields. `fact_provenance` is a per-fact map; `llm_fallback` records whether the
fallback was enabled, its outcome, model/API-call state, unresolved and
proposed facts, accepted/rejected facts, evidence errors, conflicts,
runnability before/after, usage, and estimated cost. Consumers should tolerate
additive fields within existing sections.

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

## Dashboard (milestone 8)

`contractbreaker dashboard` serves a local developer UI that drives the same
workflow the CLI uses. The browser never receives API keys, never runs analyzed
code, and never fabricates evidence: it renders exactly what the backend
returns.

```bash
# Serve the dashboard for a source root (loopback only by default).
contractbreaker dashboard --root path/to/project --port 8765

# Then open http://127.0.0.1:8765
```

Environment variables:

- `OPENAI_API_KEY` ? optional. Enables the server-side LLM fallback. The
  browser never receives it; the toggle is disabled in the UI when the server
  has no credentials.
- `CONTRACTBREAKER_LLM_MODEL` ? optional model override (default
  `gpt-4.1-mini`).

No other configuration is required, and no state is persisted between runs.

### Architecture

```text
browser (static HTML/CSS/JS)
   |  JSON over loopback HTTP
   v
contractbreaker.dashboard.server   stdlib http.server, static assets + routes
   |
   v
contractbreaker.dashboard.api      validation, approval tokens, path safety
   |
   v
contractbreaker.workflow            existing workflow (plan / approve / execute)
   |
   v
existing pipeline, runtime, verifiers
```

The API is a thin adapter. It does not reimplement extraction, readiness
validation, scenario selection, runtime execution, or verdict logic.

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/health` | GET | Non-sensitive service metadata. |
| `/api/sources` | GET | Python files under the allowed root. |
| `/api/plan` | POST | Read-only plan. Never executes the client. |
| `/api/execute` | POST | Executes a previously approved plan. |

### Safety properties

- **Plan-only never executes.** `/api/plan` always runs the workflow with
  `plan_only=True` and returns no runtime evidence.
- **Approval is enforced server-side.** `/api/execute` requires a
  server-issued `approval_token` and an explicit `approved: true`. The token is
  bound to the exact request, source, scenario, and constraints, and is
  consumed on use, so a replayed or duplicated submission returns `409`
  instead of executing twice.
- **Readiness is re-validated on the server.** The execute path re-runs
  preparation and refuses to execute a client that is no longer runnable. A
  disabled button in the UI is not treated as a security boundary.
- **Paths are constrained.** Sources must resolve inside the allowed root, must
  be `.py` files, and must exist. No shell command is ever built from user
  input.
- **No secrets reach the browser.** API keys stay in the server process. The
  health payload exposes only the API schema version, the allowed root, and
  whether LLM fallback is available.
- **Errors stay generic.** Unexpected failures return a code and message
  without stack traces or environment data.

### UI states

The interface renders distinct, non-color-only states for loading, empty,
ready, blocked, executed (confirmed / not reproduced / inconclusive), and
error. The causal timeline renders only events actually present in the
returned trace; expected-but-missing events stay missing.

## Using ContractBreaker with Alan

ContractBreaker ships a repository-level Alan Skill at
[`SKILL.md`](SKILL.md). The Skill teaches an Alan coding agent to drive the
existing `contractbreaker workflow` CLI instead of reimplementing analysis,
readiness, execution, or verification.

The Skill is for investigating a narrow class of Python retry/timeout bugs that
can duplicate a side effect. It is not a general repository scanner.

### Setup and registration

There is no MCP server, SDK, or Alan API integration. The Skill drives the
local `contractbreaker` CLI, so three things must exist in the environment
where the session runs:

1. **Python 3.10+ and the installed package** —
   `python -m pip install -e ".[dev]"` from the repository root. Installation
   is required, not optional: the CLI launches a subprocess that must be able
   to import `contractbreaker` and `examples`.
2. **The repository checkout**, so the agent can read `SKILL.md` and the target
   client.
3. **The Skill available to the session**, in one of two verified ways:
   - the repository is open and the agent reads `SKILL.md` directly;
   - the session lists `contractbreaker` as an installed skill
     (for example `/contractbreaker` in the composer).

Automatic discovery of a project-root `SKILL.md` is **not verified** and should
not be promised. On a Windows developer machine, installed skills live under
`%LOCALAPPDATA%\Alan\production\skills` as an `installed.json` index plus
`versions\<id>\SKILL.md`. That index is machine-managed; do not hand-edit it —
install the skill through Alan.

Verify the build with `contractbreaker workflow --help`: `--plan-out`,
`--plan`, and `--allowed-root` must be listed, otherwise the installed build is
stale.

No dashboard, API server, or MCP server is required for this path. Nothing is
exposed to the network.

### Sample request

> Investigate duplicate payments after a lost response in
> `path/to/client.py` using ContractBreaker. Plan it first and ask me before
> executing.

The agent should then run the equivalent of:

```bash
contractbreaker workflow \
  --request "duplicate payment after a lost response" \
  --source path/to/client.py \
  --allowed-root . \
  --plan-only \
  --plan-out .contractbreaker-plan.json \
  --format json
```

`--allowed-root` confines the analyzed source to that directory. The CLI
resolves the path first (following symlinks and collapsing `..`) and rejects
anything outside it with exit code `4`. `--plan-out` writes the plan artifact
that approval is bound to.

Note that the bundled `examples/payments/client.py` is deliberately a *partial*
example: its HTTP call sits inside a method and its endpoint, timeout, and
retry limit are not literals. Planning it therefore reports `blocked` with
concrete blockers. That is the intended behavior, and a useful demonstration
of the blocker path.

### Plan-bound approval

`--plan-out` records the investigation that was reviewed: the resolved source
path and its SHA-256, the scenario, the execution-relevant options
(`server_idempotency`, `retry_statuses`, `server_error_status`,
`execution_timeout`, `llm_fallback`), the allowed root, and the tool version —
all under a SHA-256 digest.

`--approve` **requires** `--plan`. Before anything executes, the CLI:

1. verifies the artifact's digest, so a hand-edited plan is rejected;
2. re-runs the planning pass without executing, and compares every bound field
   against the current source and options;
3. refuses to execute if the source content changed, the scenario or any
   option differs, the allowed root differs, the tool version differs, or the
   plan was not runnable.

Rejections exit `3` and print `error: approval rejected: ...`; `--approve`
without `--plan` is also exit `3`. This binds approval to *state*, not to time:
a plan stays valid while the source and options are unchanged, and it is not
one-shot. It is **not authentication** — anyone who can run the CLI can create
and approve their own plan.

At the execution boundary the runtime reads the source once, checks its SHA-256
against the approved digest, and writes those exact bytes into the temporary
runtime directory; the worker executes that copy. A mismatch aborts with
`execution.failure.kind = "source_mismatch"` before anything runs, so the bytes
that execute are the bytes that were hashed. The execution pass is also pinned
to the verified scenario, so it can not re-interpret the request into a
different one (relevant when `--llm-fallback` is enabled).

### The workflow

1. **Plan** — the agent runs `--plan-only --plan-out` and reads
   `analysis_snapshot.hypothesis.scenario_readiness` (`runnable`, `blockers`,
   `unknowns`), the extracted facts with `fact_provenance`, and the extraction
   warnings. No analyzed code runs.
2. **Explain** — the agent shows the selected scenario, the facts, the
   blockers, the resolved source path, the plan digest, what would execute, and
   what evidence would be collected.
3. **Explicit approval** — the agent stops and asks for approval of that
   specific plan artifact. General enthusiasm, an earlier unrelated approval,
   or a request to *create* the plan is not approval to execute.
4. **Execute** — only after approval, and only if the plan is runnable, the
   agent runs `--approve --plan` and reads `analysis.verdict`,
   `analysis.verdict_reason`, and
   `analysis.execution.observed_side_effects`.
5. **Report** — the agent separates static extraction facts from runtime
   observations, quotes the verifier verdict and side-effect count unchanged,
   and includes the trace IDs, blockers, and limitations.

The quote below is the actual shape of a completed run (abridged):

```text
verdict: confirmed
verdict_provenance: runtime_verifier
observed_side_effects: 2
```

### Limitations of this integration

- **Conversational approval is an instruction-level gate, not a security
  boundary.** It depends on the agent following `SKILL.md`. The CLI adds
  plan-bound approval, which guarantees that what executes matches the artifact
  that was reviewed, but it is still not authentication: anyone who can run the
  CLI can mint and approve their own plan.
- **Source containment is opt-in.** `--allowed-root` is enforced when supplied,
  after resolving symlinks and `..`. Without it the CLI honours any readable
  path, so untrusted or automated use must pass it — the Skill always does.
- **Subprocess execution is constrained, not a hardened sandbox.** The analyzed
  client runs with the invoking user's privileges. Socket construction is
  blocked and the AST contract restricts imports and calls, but nothing
  provides OS-level isolation, and the process can still consume CPU and memory
  until the execution timeout.
- **The dashboard has no authentication.** It binds to loopback by default and
  its approval token is a single-use state boundary for its own API, not an
  authorization layer. Do not expose it publicly.
- **End-to-end Alan integration has not been verified in a real session.** The
  Skill and the commands in this section are based on the repository's verified
  CLI behavior, but the Alan-side experience is provisional until a real
  session succeeds.

### Model and demo policy

- Keep `--llm-fallback` **disabled** for the judged demo. It is off by default.
- The fallback's default model is a proprietary one. The hackathon rules say
  "Alan + open models"; do not enable proprietary model calls unless the
  organizers confirm they are permitted.
- Never put `OPENAI_API_KEY` or any other secret in a report, a prompt, or a
  committed file. Do not upload or clone a repository into a cloud environment
  without the user's awareness and authorization.

## LLM extraction fallback (milestone 6B)

The optional fallback helps interpret unresolved retry, timeout, and
idempotency facts. It **proposes extraction facts only**:

- It cannot issue `confirmed`, `not_reproduced`, or `inconclusive`.
- It cannot remove deterministic warnings or widen the executable-client
  contract.
- It cannot make a hypothesis scenario-ready directly. The existing hypothesis
  builder, runtime contract, scenario readiness checks, and causal verifier
  remain the final gates.

The fallback is disabled by default. Deterministic analysis never imports the
OpenAI SDK and never makes an API request unless `--llm-fallback` is used.

### Configuration

Install the optional SDK extra:

```bash
python -m pip install -e ".[dev,llm]"
```

Set credentials and optional model configuration through the environment:

```bash
export OPENAI_API_KEY="..."
export CONTRACTBREAKER_LLM_MODEL="gpt-4.1-mini"
```

Never put a real key in `.env.example` or a report. The API key is read only from
`OPENAI_API_KEY`; it is never logged, printed, or serialized. `.env.example`
contains placeholders only. ContractBreaker does not load `.env` automatically.

The default model is `gpt-4.1-mini`. Requests use strict JSON Schema Structured
Outputs, a 20-second timeout, and no automatic SDK retries. Missing credentials,
missing SDK, auth errors, rate limits, timeouts, refusals, and malformed output
produce a structured fallback outcome and preserve the deterministic analysis.

### Merge policy

1. Run the deterministic AST extractor.
2. Identify unresolved facts (`unknown`, `ambiguous`, or `unsupported`).
3. If fallback is enabled, send a bounded source excerpt and those unresolved
   facts to OpenAI. Benchmark labels, expected verdicts, and scenario answers are
   never included in the prompt.
4. Validate each proposal's fact type, value, status, line range, and exact
   source excerpt.
5. Apply only `known` proposals for a deterministic `unknown` fact.
6. Preserve all deterministic `known`/`absent` facts. Disagreement is recorded
   as a conflict and rejected.
7. Ambiguous/unsupported deterministic facts and unsupported runtime patterns
   remain non-runnable. LLM proposals for them are advisory at most.
8. Rebuild the client hypothesis and reapply the existing runtime contract,
   readiness checks, and runtime verifier.

Accepted facts receive per-fact provenance `llm_assisted`. The report includes
`hypothesis.fact_provenance` and `hypothesis.llm_fallback` with the fallback
outcome, model, whether the API was called, unresolved/proposed/accepted/
rejected facts, evidence-validation errors, conflicts, runnability before/after,
token usage, and estimated cost when configured.

Cost is reported only when both `CONTRACTBREAKER_LLM_INPUT_USD_PER_1M` and
`CONTRACTBREAKER_LLM_OUTPUT_USD_PER_1M` are set. Token usage is reported whenever
the API supplies it. No pricing table is hardcoded.

### Privacy and limits

When `--llm-fallback` is enabled, a bounded source excerpt and unresolved-fact
metadata are sent to the configured OpenAI model. The report includes the
evidence excerpts needed to audit validation, but not the full source or API
key. Do not enable the fallback for source code that must not leave your
environment.

The fallback does not support external wrapper implementations, imported
libraries, dynamic values, or multi-file behavior. If evidence is insufficient,
the correct outcome remains `unknown`, `ambiguous`, or `unsupported`.

### Offline and live tests

All ordinary tests use mocked SDK responses and require no credentials. The live
smoke test is skipped unless all of these are true:

```bash
export CONTRACTBREAKER_LIVE_LLM=1
export OPENAI_API_KEY="..."
pytest tests/test_llm_live.py -q
```

This test makes a small paid API call and is intentionally not part of the
default suite.

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

## Client-timeout scenario (milestone 11)

`--scenario client-timeout` asks a different question: after the client gives up
at a deadline, does the server still commit? It models **one** attempt and does
not require a retry loop, a retry budget, or a reusable idempotency key.

The harness suspends the request inside the payment service instead of delaying
it, so the ordering of the timeout and any later commit is fixed by
configuration rather than by a clock. Three modes are available:

| `--timeout-mode` | Server behaviour | Client sees |
| --- | --- | --- |
| `server_first` | commits and returns | 201 response |
| `client_first_then_commit` *(default)* | suspended, committed after the timeout | simulated timeout |
| `client_first_no_commit` | suspended, aborted after the timeout | simulated timeout |

**The timeout is simulated.** `client_timeout_observed` is generated by the
harness in the in-process WSGI simulation; `requests`' own `timeout` argument is
never enforced by this transport. Every report for this scenario states
`execution.timeout.simulation.real_requests_timeout_enforced: false` and carries
a limitation saying so. Harness execution timeouts are separate: they appear as
`execution.failure.kind = "harness_timeout"` with `execution.timeout = null`.

An observed late commit is reported as `observation.outcome:
"orphaned_commit"`. Whether that is a **bug** depends on the declared contract:

| `--timeout-invariant` | Meaning |
| --- | --- |
| `abort-on-timeout` *(default)* | a side effect must not be committed after the client observed a timeout |
| `allow-orphaned-commit` | the server may finish after the client gives up; the late commit is permitted |

`confirmed` is produced only when the declared invariant is violated. The same
trace yields `confirmed` under `abort-on-timeout` and `not_reproduced` under
`allow-orphaned-commit`. The report names the invariant, its source
(`cli` or `scenario_default`), and `violated`.

## Setup

Requires Python 3.10+.

Deterministic analysis needs only the base dependencies. For the optional LLM
fallback, install the `llm` extra and set `OPENAI_API_KEY` and optionally
`CONTRACTBREAKER_LLM_MODEL`; see `.env.example`.

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

The complete suite currently collects 161 tests: 160 pass and the optional paid
live OpenAI smoke test is skipped by default.

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

`tests/test_cli.py` adds 21 CLI/report cases:

- Buggy and protected clients for both runtime scenarios.
- Automatic lost-response selection.
- Unknown retry limit and unknown timeout produce inconclusive exit `2`.
- Unsupported clients and ambiguous idempotency keys never become confirmed.
- Invalid scenarios and invalid argument combinations exit `3`.
- Missing files and output-write failures exit `4`.
- JSON schema stability, JSON-native serialization, output-file writing, and
  invocation through `python -m contractbreaker` from outside the repository.
- LLM fallback is opt-in, makes no API call when disabled, and flows validated
  facts through to the runtime verdict.

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

`tests/test_benchmark.py` adds 11 benchmark cases:

- Known metric arithmetic and denominators.
- Development/held-out CLI JSON, output files, and unsupported non-execution.
- Labels cannot change the runtime verdict.
- Per-case analysis errors are recorded, not omitted.
- Missing/invalid datasets fail with exit `2`.
- Missing `--dataset` is invalid CLI usage.
- Dataset sources cannot escape their split directory.
- LLM-assisted benchmark results are reported separately, with usage/cost and
  fallback completion status.

`tests/test_llm_fallback.py` adds 18 mocked fallback cases:

- Valid evidence fills an unknown timeout and flows into runtime verification.
- Missing, mismatched, and out-of-range evidence is rejected.
- Hallucinated retry limits and deterministic-fact conflicts are rejected.
- Disabled fallback makes zero API calls.
- Missing credentials, timeouts, rate limits, refusals, malformed output, and
  invalid top-level responses are handled without crashing deterministic analysis.
- Ambiguous facts and unsupported runtime patterns remain non-runnable.
- LLM-filled facts cannot force a `confirmed` verdict.
- Model configuration and token-cost estimation.
- Benchmark prompts contain no labels or expected verdicts.

`tests/test_workflow.py` adds 32 orchestration cases:

- Deterministic scenario selection, structured overrides, and source resolution.
- Ambiguous, underspecified, and unsupported requests are blocked.
- Plan-only and unapproved runs never execute the client.
- Blocked, unsupported, and nested clients never reach execution.
- Conflicting LLM facts stay visible and do not force execution.
- Approved buggy/fixed and retryable-error runs preserve the verifier verdict.
- Execution failures and incomplete traces never become `confirmed`.
- LLM interpretation requires a verbatim phrase from the request.
- The summarizer cannot change the verdict or invent side-effect counts.
- LLM-disabled workflows never construct an LLM client.
- Workflow JSON is JSON-native and never embeds the analyzed source text.

`tests/test_dashboard.py` adds 28 backend and HTTP tests:

- Plan-only responses never contain runtime evidence.
- Blocked and nested clients are never executable.
- Approval tokens are required, single-use, and rejected when replayed.
- Readiness is re-validated on the server, independently of client state.
- Path traversal, out-of-root paths, non-Python files, and missing files are
  rejected.
- Invalid scenarios, constraints, and oversized requests are rejected.
- Health, plan, and execute responses never expose API keys or environment
  variables.
- The HTTP layer serves the UI assets, blocks asset traversal, rejects
  malformed bodies, and completes a plan-then-execute cycle.

`tests/test_llm_live.py` is skipped unless `CONTRACTBREAKER_LIVE_LLM=1` and
credentials are present.

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

Benchmark reports keep `benchmark_schema_version` at `"1.0"`. Milestone 6B adds
optional top-level `llm_assisted_splits` and `llm_fallback` sections plus
`deterministic_all_matched`; these are additive fields.

Each manifest case labels:

- Inputs passed to the normal pipeline: scenario, server idempotency, retry
  statuses, server error status, and execution timeout.
- Expected extraction status and labeled facts.
- Expected warning codes.
- Expected runnability, whether execution should occur, verdict, and
  side-effect count.
- Optional `expected_llm` labels used only for the LLM-assisted scoring pass
  when evidence can resolve a deterministic unknown (currently the unknown
  timeout case). `expected_llm` is never included in the LLM prompt.

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

# Also evaluate the LLM-assisted pass. This may make paid OpenAI calls.
contractbreaker benchmark --dataset benchmark --llm-fallback --format json
```

Benchmark exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Every requested pass matched. |
| `1` | Completed with one or more mismatches. |
| `2` | Dataset, execution, or requested LLM pass could not be completed. |
| `3` | Invalid CLI arguments. |

When `--llm-fallback` is requested, the report includes both deterministic
`splits` and `llm_assisted_splits`, plus an aggregate `llm_fallback` summary
with API-call count, outcomes, token usage, estimated cost, accepted/rejected
facts, conflicts, and invalid-evidence rejections. If the LLM pass is
unavailable, the deterministic results are still reported, but the benchmark
exits `2` because the requested comparison did not complete.

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

LLM-assisted pass metrics use the same definitions, scoring against
`expected_llm` when present and `expected` otherwise. Additional LLM summaries
report API calls, fallback outcomes, accepted/rejected facts, evidence
rejections, conflicts, token usage, and estimated cost. LLM cases with
`unavailable` or `invalid_output` outcomes are marked incomplete and are not
counted as successful LLM-assisted evaluation.

### Current synthetic results

All 20 development, 10 held-out, and 12 independent-validation labeled cases
matched their expected labels, and all 7 evidence-integrity cases produced the
expected verdict.

| Split | Cases | Status | Facts | Warnings | Uncertainty | Runnable/blocked | Incorrectly allowed | Detection | False positives | Side effects | Inconclusive |
| --- | ---: | --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: |
| Development | 20 | 20/20 | all labeled | 20/20 | 8/8 | 15/5 | 0 | 6/6 | 0/8 | 15/15 | 6 |
| Held-out | 10 | 10/10 | all labeled | 10/10 | 4/4 | 4/6 | 0 | 1/1 | 0/3 | 4/4 | 6 |
| Independent validation | 12 | 12/12 | all labeled | 12/12 | 13/13 | 9/3 | 0 | 4/4 | 0/5 | 9/9 | 3 |

Held-out inconclusive cases: `held-lost-while-buggy`,
`held-lost-retry-wrapper`, `held-retry-fresh-key`, `held-timeout-zero`,
`held-unsupported-tenacity`, `held-parse-error`.

### Independent validation and evidence integrity (milestone 12)

The benchmark reports three splits plus a separate integrity suite; it no
longer emits a single aggregate:

- `development` and `held_out` are the original synthetic regression fixtures.
- `validation` holds 12 independently authored compatibility fixtures. They are
  **not public real-world client code**, and passing them does **not**
  demonstrate generalisation to public clients. They were written for this
  repository with different control flow, binding style, and constants from the
  development fixtures, including matched buggy/fixed pairs that differ only in
  the reliability behaviour under test.
- `integrity` stores 7 event traces that are replayed through the **production
  verifiers**. Corrupt or contradictory traces must stay `inconclusive`, and the
  suite includes a positive control that must still be `confirmed`.

Per split, cases are partitioned into four cohorts, each with its own
denominator: `supported_buggy` (detection, missed-bug rate), `supported_fixed`
(false-positive rate), `unsupported_blocked` (readiness accuracy), and
`expected_inconclusive` (unexpected verdicts). No combined accuracy number is
reported.

`integrity.all_passed` and `integrity.false_confirmed` are reported separately
from `all_matched`, which covers only the three splits. The CLI exits non-zero
if either the splits or the integrity suite fails.

Two held-out fixtures were replaced in this milestone because their files were
byte-identical to development fixtures (`held_lost_retry_wrapper.py` and
`held_retry_policy_disagree.py`). Their expected verdicts, readiness, and side
effect counts are unchanged; only the source text is now independent.

An offline fake-provider LLM-assisted pass was also run through the same
benchmark to validate the comparison path. The fake provider is **not a live
model measurement**; it only proposes the locally evidenced timeout for
`dev-unknown-timeout` and nothing else.

| LLM-assisted pass (offline fake provider) | Cases | Status | Facts | Runnable/blocked | Incorrectly allowed | Detection | False positives | Side effects | Executed verdicts | Inconclusive |
| --- | ---: | --- | --- | --- | ---: | --- | --- | --- | --- | ---: |
| Development | 13 | 13/13 | 12/12 each | 8/5 | 0 | 3/3 | 0/4 | 8/8 | 8/8 | 6 |
| Held-out | 10 | 10/10 | 9/9 each | 4/6 | 0 | 1/1 | 0/3 | 4/4 | 4/4 | 6 |

This table reflects the milestone-6A development split (13 cases) and was not
re-run against the current 20-case development split.

The fake pass made 10 fake API calls, proposed one fact, accepted it, and
recorded no conflicts or invalid-evidence rejections. Token usage was supplied
by the fake provider; cost was `null` because no pricing rates were configured.
The improvement from resolving `dev-unknown-timeout` is a demonstration of the
measurement path, not a claim about model quality.

No live OpenAI benchmark or smoke call was run for this report because the
explicit `CONTRACTBREAKER_LIVE_LLM=1` opt-in was not set. The live smoke test is
present but skipped by default. A live model result must be labeled separately
from these offline/synthetic numbers.

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
- **The LLM fallback is not a source of truth.** It only proposes facts. Every
  accepted fact requires validated exact source evidence; conflicts, ambiguity,
  unsupported facts, runtime-contract failures, and verifier outcomes remain
  authoritative. LLM confidence is never used as evidence.
- **LLM fallback sends source context to OpenAI.** When enabled, a bounded source
  excerpt and unresolved-fact metadata leave the local environment. Reports
  include evidence snippets needed for audit, but never API keys or the full
  source. Do not enable fallback for code that must remain local.
- **LLM cost depends on the account and model.** Token usage is reported when
  available. Estimated cost is reported only if per-million input/output rates
  are explicitly configured; no pricing table is assumed.
- **No live model benchmark was run.** The documented LLM-assisted benchmark
  numbers use a clearly labeled offline fake provider. The live smoke test is
  skipped unless explicitly enabled with credentials. Offline fake results are
  not evidence of OpenAI model accuracy or improvement.
- **Workflow scope is intentionally narrow.** Request interpretation supports
  two scenarios, a small unsupported-topic list, and a `.py` source path. It is
  not a general task planner and does not orchestrate multiple agents.
- **Workflow interpretation can still be wrong.** Keyword mapping is
  deterministic and conservative, but a request can mention a scenario without
  meaning it. Inspect the plan and blockers before approving execution.
- **LLM workflow stages send request text, not just source.** When
  `--llm-fallback` is enabled, the request text may be sent for interpretation
  and the report payload may be sent for summarization. Neither stage can change
  the verdict; both can be disabled.
- **Reports are scenario evidence, not a security audit.** Unsupported patterns,
  unknown facts, and non-runnable hypotheses are reported as such and can never
  become `confirmed`.
- **Not a safety proof.** A `not_reproduced` verdict says this deterministic
  scenario did not produce the duplicate; it does not prove the guard is
  correct for every payload, key scope, or deployment topology.
- **The dashboard is a local developer tool.** It binds to loopback by
  default, has no authentication, and is not hardened for exposure on a shared
  network. Do not bind it to a public interface without adding access control.
- **The dashboard does not persist history.** There is no database, no
  investigation store, and no uploaded-file retention. A plan's approval token
  lives in server memory only and is lost when the server restarts.
- **Not implemented yet:** Alan integration; no multi-agent or autonomous LLM
  extraction framework is present.
- **Idempotency-key stability is observed at runtime.** `execution.idempotency_key`
  reports `stable`, `changing`, `absent`, or `insufficient_evidence` from the key
  recorded on each attempt, alongside - never instead of - the AST expectation,
  which keeps its own provenance. Keys are compared as truncated SHA-256
  fingerprints; raw header values are never written to the report. A single
  keyed attempt cannot establish stability and is reported as insufficient
  evidence.
- **Server deduplication is reported separately from client key behaviour.**
  `execution.server_deduplication` records the configured server behaviour, its
  provenance, and the number of replays actually observed. `--server-idempotency
  auto` is reported as `inferred_from_client_facts` with `inferred: true`: a
  `not_reproduced` verdict that depends on it is conditional. Use `on` or `off`
  for independent evidence. A stable client key does not prove the server
  enforces deduplication, and a deduplicating server does not prove the client
  reuses its key.
- **Unstable keys are testable.** A client whose key is known to change between
  attempts is now runnable, because that is the behaviour the experiment
  evaluates. Execution is still blocked when key presence or reuse is
  unresolved, or when the runtime contract is unsupported.
- **Client timeouts are simulated, not enforced.** The in-process WSGI transport
  never honours a client's `requests` timeout. Only `--scenario client-timeout`
  evaluates timeout behaviour, and it injects the timeout itself at a fixed
  ordering point. The report always records
  `real_requests_timeout_enforced: false`.
- **A timeout is not automatically a bug.** The client-timeout scenario compares
  the observed sequence against a declared invariant. An `orphaned_commit` is
  `confirmed` only under an invariant that forbids it; under
  `allow-orphaned-commit` the same observation is `not_reproduced`.
- **Harness execution timeouts are not client timeouts.** A worker killed by
  `--execution-timeout` produces `execution.failure.kind = "harness_timeout"`
  with no timeout evidence; it is never reported as a simulated client timeout.
- **The validation split is independently authored, not real-world.** Its 12
  fixtures are compatibility cases written for this repository. No public client
  has been validated, and passing this split does not show that ContractBreaker
  generalises to public client code.
- **No public client currently fits the runtime contract.** Every public SDK
  client examined conceptually violates at least two rules (module-level call,
  `POST /payments`, literal timeout). They are blocked rather than modified or
  forced through the harness; no such code was downloaded or executed.
- **Evidence integrity is scored separately.** The 7 stored traces are replayed
  through the production verifiers. This is regression evidence for the
  verifiers, not a fuzzing campaign, and it does not cover every malformed trace
  shape.
- **Benchmark categories are not a single score.** Detection, missed bugs,
  false positives, readiness errors, inconclusive outcomes, and execution
  failures keep separate denominators; the dataset remains small (42 split
  cases plus 7 integrity traces).
