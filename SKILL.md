---
name: contractbreaker
description: "This skill should be used when an Alan session must investigate a Python client for retry/timeout bugs that can cause duplicate side effects, such as a lost response after a server commit, or a retryable HTTP error that the client retries. It drives the ContractBreaker CLI: plan the investigation, obtain explicit human approval, execute the supported scenario, and report the causal verifier's verdict with runtime evidence. Do not use it for general repository scanning, for clients outside the documented runtime contract, or to bypass the approval step."
version: 1.0.0
---

# ContractBreaker

## Purpose and scope

ContractBreaker investigates a narrow class of Python client reliability bugs:
a client that retries a request after the server already committed a side
effect, so one logical operation produces two side effects.

Two scenarios are supported, and only these two:

- **lost-response** — the server commits, the response is lost, the client
  retries, and the server commits again.
- **retryable-error** — the server commits, then returns a retryable HTTP error
  (by default `503`); the client retries because that status is in its policy,
  and the server commits again.

ContractBreaker is **not** a general repository scanner. It analyzes **one**
Python client file at a time, and it can only *execute* a narrow class of
clients. See [Runtime contract](#runtime-contract) below.

Use the `contractbreaker` CLI. Never reimplement extraction, readiness checks,
scenario execution, or verdict logic, and never infer a verdict yourself.

## Preflight

1. Confirm you are in the ContractBreaker repository and that Python is
   available.
2. Check the CLI is installed:

   ```bash
   contractbreaker --version
   ```

   If that command is missing, `python -m contractbreaker --version` should
   work from the repository root. If neither works, the package is not
   installed. **Ask the user before installing anything.** The documented
   install is:

   ```bash
   python -m pip install -e ".[dev]"
   ```

   Use `".[dev,llm]"` only if the user wants the optional LLM extra. Never
   install, upgrade, or otherwise modify the environment without the user's
   explicit go-ahead.
3. Identify the **target source file** (a single `.py` client) and the
   **requested scenario**. If the user names a scenario, use it. Otherwise
   let the workflow interpret the request; do not pick a scenario yourself.
4. Check the target against the runtime contract. If it is clearly outside it
   (for example a `httpx` client, a call inside a function or class, a
   non-`POST` request, or a URL path other than `/payments`), say so plainly
   instead of promising an execution.

## Plan first

Always plan before executing. Plan-only mode never runs the analyzed code:

```bash
contractbreaker workflow \
  --request "duplicate payment after a lost response" \
  --source path/to/client.py \
  --plan-only \
  --format json
```

Add `--scenario lost-response` or `--scenario retryable-error` to pin the
scenario. `--request` may be omitted only when both `--scenario` and
`--source` are given.

Exit code `2` with `"outcome": "planned"` is the **expected** result of a
successful plan. It is not an error.

Read these fields from the JSON document:

| Field | Meaning |
| --- | --- |
| `outcome` | `planned`, `blocked`, `executed`, or `failed` |
| `approval` | `pending` or `approved` |
| `stages[]` | per-stage `stage`, `outcome`, `summary`, `blockers` |
| `analysis_snapshot.hypothesis.scenario_readiness.runnable` | whether execution is possible |
| `analysis_snapshot.hypothesis.scenario_readiness.blockers[]` | why it is blocked |
| `analysis_snapshot.hypothesis.scenario_readiness.unknowns[]` | facts that are not resolved |
| `analysis_snapshot.hypothesis.fact_provenance` | origin of each extracted fact |
| `analysis_snapshot.extraction.warnings[]` | unsupported or ambiguous patterns |
| `analysis` | `null` during a plan — there is no runtime evidence yet |
| `request.constraints` | the constraints in effect for this plan |
| `limitations[]` | scope and simulation caveats |

The effective runtime `analysis.scenario_configuration` exists only after
execution; during planning, read the scenario and constraints from
`request.scenario` and `request.constraints`.

Then tell the user, in plain terms:

- which scenario was selected and how it was selected
  (`request.scenario_source`),
- the extracted facts with their provenance, separating known facts from
  unknown or ambiguous ones,
- the readiness state and every blocker,
- **what would execute** and **what evidence would be collected** (which
  side effect would be committed, what fault would be injected, and that the
  client is run in a subprocess against a local in-process test service).

If `runnable` is `false`, or `outcome` is `blocked`, **stop**. Explain the
blockers and what would need to change. Do not attempt a workaround, do not
switch scenario to force a run, and do not execute.

## Explicit approval

Only when the plan reports `runnable: true`:

1. Show the proposed investigation: scenario, target file, extracted facts,
   the fault that will be injected, and the constraints in effect.
2. State the execution consequences plainly: the analyzed client will be
   executed in a subprocess, a side effect may be committed twice, and the
   result is a runtime verdict, not a lint result.
3. **Ask for explicit approval of that specific plan.** Stop and wait.

Approval rules:

- Approval must refer to the plan you just displayed. General enthusiasm, a
  request to "create the plan", or approval given for an earlier unrelated
  investigation is **not** approval to execute.
- If the source file, scenario, or constraints change after approval, the
  approval is void. Produce a new plan and ask again.
- Use the CLI's own approval mechanism: the `--approve` flag. Do not work
  around it, and do not run the approved command speculatively.

Be honest about the strength of this gate: **conversational approval is an
instruction-level boundary, not a security boundary.** It relies on the agent
following this skill. The `contractbreaker dashboard` enforces a stronger
server-issued approval token, and is the better surface for untrusted input.

## Execute and report

Only after the user explicitly approved the displayed plan:

```bash
contractbreaker workflow \
  --request "duplicate payment after a lost response" \
  --source path/to/client.py \
  --approve \
  --format json
```

Read the actual result. The verdict lives in the nested `analysis` object:

| Field | Meaning |
| --- | --- |
| `analysis.verdict` | `confirmed`, `not_reproduced`, or `inconclusive` |
| `analysis.verdict_reason` | the verifier's own wording |
| `analysis.verdict_provenance` | `runtime_verifier` — the causal verifier decided |
| `analysis.execution.occurred` | whether the client actually ran |
| `analysis.execution.observed_side_effects` | observed side-effect count |
| `analysis.execution.events[]` | the causal trace, in order |
| `analysis.execution.attempt_ids[]` | attempt identifiers |
| `analysis.execution.operation_ids[]` | logical operation identifiers |
| `analysis.execution.side_effect_ids[]` | unique per side effect |
| `analysis.execution.runner` | which runtime produced the trace |
| `analysis.limitations[]` | scope and simulation caveats |
| `explanation` | the evidence-based explanation |
| `explanation_meta.used_llm` | whether an LLM produced the summary |

Workflow exit codes mirror the verdict: `0` = `not_reproduced`, `1` =
`confirmed`, `2` = inconclusive, planned, or not runnable, `3` = invalid or
underspecified request, `4` = unsupported request or input.

Reporting rules:

- **Surface the verifier's exact verdict and side-effect count.** Quote
  `analysis.verdict` and `analysis.execution.observed_side_effects`; do not
  paraphrase them into something stronger or weaker.
- **Separate static facts from runtime observations.** Extraction facts and
  provenance come from the source; the trace, attempt IDs, and side-effect
  count come from the run. Label which is which.
- **Include the evidence, the blockers, and the limitations** that bear on the
  result.
- **Never invent** trace events, source excerpts, test results, or a verdict
  that the report does not contain. If `analysis.execution.occurred` is
  `false` or the trace is empty, say that execution did not happen.
- **Never change** `confirmed`, `not_reproduced`, or `inconclusive` because of
  your own opinion, a model's opinion, or how the code "looks". Only the
  runtime causal verifier sets the verdict. If the outcome looks wrong, report
  the verdict and explain the evidence, then suggest a separate change — do
  not relabel the result.
- If execution failed or the trace is incomplete, report the actual outcome
  (usually `inconclusive`) and the reason.

## Runtime contract

Only these clients can be **executed**. Anything else is reported as blocked,
not worked around.

- Exactly one recognized `requests` call site.
- The call must run at module level — module-level loops, conditionals, and
  `try` blocks are fine; a function, class, or lambda body is not.
- `POST` to a URL path of exactly `/payments`.
- Allowlisted imports: `requests`, `uuid`, `json`, `typing`, `dataclasses`,
  `datetime`, `time`, `secrets`, `base64`, `hashlib`.
- A stable idempotency key across retries, or no key at all. A conditional key
  is ambiguous and blocks execution.

Static extraction is broader than execution, so a file can be *analyzed* while
still being non-runnable. Report that distinction rather than treating
analysis success as readiness.

## Model and privacy policy

- **Keep `--llm-fallback` disabled by default.** It is off unless explicitly
  enabled, and the deterministic extractor handles the supported patterns.
- **Do not enable proprietary [OI] model calls in a judged or shared demo**
  unless the event organizers have confirmed they are permitted. The event
  rules are "Alan + open models"; the fallback's default model is a
  proprietary one. When a fallback is genuinely wanted, say plainly that a
  bounded source excerpt and unresolved-fact metadata will be sent to an
  external model, and get the user's agreement first.
- **Never print, log, or report API keys or environment variables.** The key
  is read only from `OPENAI_API_KEY` and never appears in a report.
- **Do not upload or clone a repository into a cloud environment** without the
  user knowing and authorizing it. Prefer the "This computer" execution
  target so source stays local.
- The analyzed client runs in a subprocess against an in-process test service.
  This is deterministic test isolation, **not a hardened security sandbox**.
  Do not describe it as one.

## Reference

- `contractbreaker workflow --help` — full options and exit codes.
- `contractbreaker analyze --help` — single-shot analysis without approval.
- `contractbreaker benchmark --help` — labeled development/held-out scoring.
- `contractbreaker dashboard --help` — local UI with a server-issued approval
  token.
- `README.md` — full documentation, report schema, and known limitations.
