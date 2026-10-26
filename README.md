# ContractBreaker

Test-proven detection of retry/timeout conflicts between a client and a service.

> Status: milestone 1 (hackathon build in progress).

## The problem
Each side of an integration can be correct alone and still break together. A client
retries after a timeout; the server had already committed the first request. Result:
a duplicate side effect (double charge). Types, schemas and unit tests all pass.

## Approach
1. Run the client against the real service in-process with a fault injector that
   drops the response *after* the server commits.
2. Record an ordered event log (request, commit, dropped response, retry, commit).
3. Report a finding only if the predicted causal sequence occurred AND the
   invariant ("one logical operation, one side effect") broke.

Three honest outcomes: `confirmed`, `not_reproduced`, `inconclusive`.

## Run it
    pip install -e ".[dev]"
    python demo.py
    pytest

## Related work (what this is NOT)
Pact and Schemathesis cover explicit contracts and schema-based fuzzing; fault
proxies such as Toxiproxy can reproduce timeouts by hand. This project is a small
experiment in automating the scenario and requiring causal evidence before reporting.
(Add verified citations here.)

## Roadmap
- [x] Seeded duplicate-payment scenario, causal verdicts
- [ ] AST extraction of retry/timeout settings
- [ ] Second, distinct scenario
- [ ] CLI + structured report
- [ ] Hosted report viewer
