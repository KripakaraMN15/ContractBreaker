# ContractBreaker

Deterministic detection of retry/timeout conflicts between an HTTP client and a
service, backed by a causal event trace rather than a pass/fail assertion.

> Status: milestone 2.1. The build verifies one response-loss scenario, extracts
> narrow client retry/timeout facts, and derives deterministic hypotheses that
> can be mapped onto the reference scenario. Static extraction never issues a
> verdict.

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

## Reference payment scenario

`examples/payments` contains a small payment client and Flask service:

- The client retries timeouts and optionally sends a stable
  `Idempotency-Key` plus a stable `X-Operation-Id`.
- The service optionally deduplicates known idempotency keys and appends every
  committed payment to a ledger.
- `examples/payments/scenario.py` wires both sides to the fault adapter and
  returns a verifier `Report`.

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

The test suite is deterministic and does not require network access.

## Tests

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

## Known limitations

- **Simulated failures only.** The adapter routes `requests` calls into a Flask
  test client and raises a `ReadTimeout` after in-process server execution. It
  does not inject real network-level failures, packet loss, or a real dropped
  HTTP response. The boundary being modeled is "server committed, client saw a
  timeout", not an actual socket-level fault.
- **Single runtime scenario.** Only the duplicate-side-effect retry conflict is
  covered by the causal verifier.
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
- **Hypothesis scope.** A runnable hypothesis requires exactly one recognized
  call, a literal positive timeout, a known retry count, an identified
  timeout/connection trigger, and a stable or absent idempotency key.
- **Not a safety proof.** A `not_reproduced` verdict says this deterministic
  scenario did not produce the duplicate; it does not prove the guard is
  correct for every payload, key scope, or deployment topology.
- **Not implemented yet:** LLM fallback, the second scenario, dashboard, and
  Alan integration.