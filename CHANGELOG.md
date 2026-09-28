# Changelog

## 0.1.0a2 — 2026-09-22

### Implemented

Native Next.js sample loading, request edits, explicit quote confirmation, rejection and scope reset; shared authenticated fetch/SSE transport; authenticated synthetic samples and capability metadata; explicit CORS origins; demo/real-ERP separation; strict model message validation and comparison-tool prerequisite; redacted-by-design tool event metadata; read-only ERP preflight; persisted ERP draft readback verification.

### Tested

111 Python passes, 2 environment skips; 14 frontend transport unit passes; 3 shared transport real-HTTP passes; original 14-step HTTP/worker smoke passed. Two SIGKILL scenarios (included in the 111) use separate processes and a persistent simulated ERP, with lease expiration injected by the test.

### Not accepted / not executed

Strict static browser gate fails twice due managed environment policy. Native Next dependency installation failed with registry DNS resolution error; strict native build/E2E gate is blocked. Four native browser cases are authored, not executed. No live ERPNext, live model, PostgreSQL, Docker or remote CI acceptance. Still local Alpha, not a complete MVP.

## 0.1.0a1 — historical baseline

User-supplied initial source package: deterministic business core, four fixed-layout import formats, snapshot-bound independent approval, operation ledger/outbox and persistent mock ERP. Prior reports retained. Baseline test rerun: 80 passed, 2 skipped; added counterexamples reproduced demo/ERP configuration and null model-message defects subsequently fixed in a2.
