# Native candidate stack gate (scripted provider)

This continuation keeps the 57a1b54 candidate queue and all existing business
services. It closes an integration gap: the earlier native candidate browser tests
intercepted queue receipts, while the real separate Worker fixture used SQLite.
This gate runs the real Next production container, same-origin API, PostgreSQL,
independent candidate Worker and browser in one environment. Browser business
responses are not intercepted. The provider is **scripted**, never a paid LLM.

## Boundaries

- Four synthetic requests: normal/imported, cancelled before claim, cancelled
  during a held call, and an actual SIGKILL of a running Worker.
- Exactly four scripted provider requests expected: two normal plus one for each
  interrupted in-flight case. No automatic replay after kill.
- Lease expiry is explicitly injected into the disposable PostgreSQL database.
  SIGKILL itself is real; this is not a wall-clock lease test.
- A buyer imports one candidate as an unconfirmed quote. No quote confirmation,
  independent approval, ERP operation, purchase order or payment is performed.
- API has no provider key; Worker configuration must match the literal local
  fixture model/key/URL before testing. Provider namespace is shared only with
  the Worker, no provider port published, no arbitrary upstream/proxy.
- No real-model quality, semantic citation support, cost or general network
  sandbox/security claim follows from these tests.
- Record check is a fail-closed consistency check, not a separate experiment.

## Reproduce in a NEW disposable working directory only

Use the exact steps in `.github/workflows/candidate-stack.yml`. Set
`COMPOSE_PROJECT_NAME=pf-candidate-<unique-suffix>`,
`PF_CANDIDATE_STACK_FIXTURE=1`, and
`PF_CANDIDATE_WORKER_ENV_FILE=.env.candidate-fixture`.
The worker env file must contain only the synthetic values written by that
workflow. Never reuse a real provider env file or production data volumes.

Combine `compose.postgres.yaml`, `compose.web.yaml`,
`compose.candidate-worker.yaml`, `compose.candidate-fixture.yaml`.
The fixture publishes no port; the UI is literal loopback port 3000.
The runner refuses non-demo/mock, non-PostgreSQL and non-empty business data.

```bash
python -m pytest -q tests/test_candidate_stack_gate.py
python scripts/verify_candidate_stack.py --fixture --output /tmp/NEW-candidate-run/roundtrip.json
python scripts/check_candidate_stack.py --report /tmp/NEW-candidate-run/roundtrip.json
```

Without `--fixture`, the runner writes blocked with network_attempted=false and
exits 2. Each output path is create-only. Use a fresh directory for each attempt.
The gate stops/starts API, web and Worker and intentionally kills one Worker;
it must never be pointed at a shared working stack. Cleanup of generated volumes
is safe only for this newly allocated Compose project.

## Delivery and acceptance

Source/configuration existence is not runtime acceptance. Inspect the exact
commit's native-candidate-stack-validation job and original roundtrip/acceptance
records. Unit tests exercise malformed evidence and the loopback fixture; their
passing result alone does not prove that Docker or the browser passed.
Historical CI evidence for 57a1b54 is separate from this continuation.
