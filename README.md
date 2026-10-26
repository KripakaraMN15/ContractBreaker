# ContractBreaker

Deterministic detection of retry/timeout conflicts between an HTTP client and a
service, backed by a causal event trace rather than a pass/fail assertion.

> Status: milestone 1. The current build verifies one scenario: a response is
> lost after the server commits, the client retries, and a second side effect
> may be committed.

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

`tests/test_duplicate_side_effects.py` covers:

- Buggy client and server: `confirmed`, two side effects.
- Idempotency protection on both sides: `not_reproduced`, one side effect.
- Client that never retries: `not_reproduced`, one side effect.
- Server supports idempotency but the client sends no key: still `confirmed`.
- Incomplete trace: `inconclusive`.
- Incorrect event ordering: not `confirmed`.
- Mismatched attempt identifiers: not `confirmed`.
- Malformed or contradictory evidence: `inconclusive`.

## Known limitations

- **Simulated failures only.** The adapter routes `requests` calls into a Flask
  test client and raises a `ReadTimeout` after in-process server execution. It
  does not inject real network-level failures, packet loss, or a real dropped
  HTTP response. The boundary being modeled is "server committed, client saw a
  timeout", not an actual socket-level fault.
- **Single scenario.** Only the duplicate-side-effect retry conflict is covered.
- **Not a safety proof.** A `not_reproduced` verdict says this deterministic
  scenario did not produce the duplicate; it does not prove the guard is
  correct for every payload, key scope, or deployment topology.
- **Not implemented yet:** AST extraction, LLM fallback, dashboard, and Alan
  integration.