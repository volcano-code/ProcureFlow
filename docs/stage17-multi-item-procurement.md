# Stage 17: bounded multi-item procurement

This is an **unpublished local development increment** from commit
`fc9750eeb5e0bf7417f16ba981a8c3bee891ebdf`, tree
`4a0a151a09afef085df55d2c284d5f291462370f`. Read-only GitHub inspection confirmed
that PR #1 still had that head before work began. The earlier source and evidence
remain immutable. No push, PR edit, merge, deployment, production access, real
model call is part of this increment. Independent security review remains incomplete.

## Supported purchase

One request has 1–20 unique, case-sensitive SKU lines, each with an explicit
quantity and EA unit. Currency is CNY. A supplier quotation must cover that exact
set, with exactly the requested quantities. A single supplier supplies the entire
basket. Duplicate SKU rows are rejected, not automatically aggregated. Split
awards, substitutions, partial orders, OCR, unit conversion, foreign currencies,
inventory reservations and purchase-order submission are outside this scope.

Legacy scalar request/quote fields remain supported. Legacy serialization does
not add a null `lines` property, preserving existing snapshots and parser inputs.
Multi request/quote contracts cannot simultaneously specify scalar item fields.
The native Next.js workbench exposes multi-item workflows; the historical static
demo remains single-item.

## API and source import

- `POST /api/v1/requests` and version-checked `PUT /requests/{id}` accept `lines`:
  `[{"sku":"STAND-01","quantity":"2","uom":"EA"}, ...]`, alongside the existing
  title, budget, currency and delivery limit. JSON numbers are not accepted for
  decimal quantities or money.
- Quote values hold quote-level `supplier_id`, `currency`, `shipping_cost`, plus
  `lines` containing `sku`, `quantity`, `uom`, `unit_price`, `tax_mode`, `tax_rate`,
  `discount`, and `delivery_days`. Missing line values remain null/unknown.
- Table preview supports exactly one legacy `row` or explicit `rows`, with at
  most 20 unique source row numbers. Existing worksheet, header, mapping and
  source-column provenance checks apply independently to every selected row.
- Multi-row CSV/XLSX sources repeat the same supplier, CNY currency and final
  quote-level freight in every selected row. Freight is charged once. Different
  header values, or a mixture of missing/present headers, are rejected. An
  all-missing header remains unknown. The software does not infer freight from
  one row and copy it into missing rows or sum repeated quote-level freight.
- A row selection on a multi-item request creates a partial one-line basket.
  Partial coverage can be imported and inspected, but cannot form a proposal.
  A known non-requested SKU is rejected at preview; a missing SKU remains
  incomplete. Missing source cells and formulas are never evaluated or set to 0.
- Import preview still has a revision, expiry and request-version binding.
  Confirming a displayed preview creates an unconfirmed quote; buyer field
  confirmation remains a separate explicit action. Same-revision import confirm
  is an idempotent readback, including after a lost response.

Each line's evidence uses `lines.N.field`; headers carry every repeated source
cell in `sources`. Fragment IDs identify the original sheet/row/column and remain
stable when selection/mapping order changes. Original bytes and their SHA-256
remain authoritative. Corrections have actor, reason and prior-version identity.
Changing the SKU at an array position marks all fields at that position as manual,
even if their numeric values happen to match the prior SKU, so evidence cannot
be borrowed from a different item.

## Costs, coverage and approval

For each line, Decimal arithmetic rounds quantity × price to cents, subtracts
its explicit goods discount, then adds explicitly excluded tax rounded to cents.
Inclusive tax does not add a second tax charge. The line totals are summed and
quote-level final gross freight is added exactly once. One incomplete line makes
the whole total unknown. No incomplete line is dropped from the calculation.

Responses include each line's goods, discount, added tax, total and violations,
plus whole-quote totals and `coverage` (requested/quoted/missing/unexpected SKUs).
Each line must meet its requested quantity, unit and effective delivery limit;
the basket total must meet the stricter request/tenant budget. The minimum valid
quote policy counts distinct suppliers whose entire confirmed basket is eligible.
A lower-priced partial basket cannot win against a complete quotation.

The complete request, ordered lines, all quote versions (including ineligible
ones), source hashes, evidence, policy, ERP target and mapping version remain
bound into the approval snapshot. Multi quotes use `multi-sku-v1`; legacy quotes
retain `single-sku-v2`. Any relevant line/header/policy/source change stales the
whole approval. No per-line approval can authorize a partial write. Independent
approver, expiry, revocation, tenant and pre-dispatch checks are unchanged.

## Drafts and independent readback

MockERP persists every line, complete cost and delivery terms, total and existing
operation/snapshot identity. Repeated reservation/dispatch uses the same key;
a response lost after commit proceeds to read-only reconciliation. Missing
readback is still not evidence of no commit and never authorizes a blind retry.

The ERPNext adapter's multi-item mapping is intentionally narrower than local
comparison. Mapping `multi-line-zero-tax-costs-v1` requires:

- 1–20 unique SKU lines in CNY/EA, integral quantities
- explicit tax rate 0 and discount 0 on every line
- one common explicit included/excluded tax mode
- each unit price, total goods, final total and quote freight within 1,000,000 CNY
- a configured freight account whenever nonzero freight is present

Positive or mixed taxes, line discounts, fractional EA quantities and out-of-range
amounts are rejected before network I/O. This does not silently reinterpret or
redistribute line taxes/discounts. The standard item description preserves bounded
commercial tax/discount/delivery terms; independent readback checks that exact
text and every line's item identity, quantity, price, unit, goods/net amounts,
pricing/discount fields and templates. Header/tax/freight components, currency,
company, date, operation key, snapshot hash and draft status also must match.
Missing, extra, duplicate, or compensated-but-mutated lines fail verification.
Readback order may differ because comparison identifies rows by unique SKU.

Both the POST result and any reconciliation use independent GET readback.
Metadata checks remain before the final authority check immediately preceding
POST. There are no submit, delete, payment or automatic resend tools. This
adapter work is verified with synthetic HTTP transport contracts only; no new
live ERPNext compatibility or account access is claimed.

## Recovery and storage

The existing JSON columns store new line collections; no schema migration is
required for this increment. Old readers do not understand new multi-item data;
application downgrade against an instance containing multi-item snapshots is
unsupported. Back up and validate upgrades on copies.

V1 paired backup bundles bind exact source fingerprints. Older bundles, including
those made at `fc9750e`, must be verified/restored using matching trusted source.
There is no cross-version backup conversion or relaxed fingerprint check here.
Existing backups/evidence were not changed. Restored operations keep permanent
recovery holds; diagnostics display per-line expected/observed values and
component differences but never release a hold, refresh an approval or replay.

## Reproduce locally

Use disposable development databases and installed pinned dependencies:

```sh
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
npm run test:unit --prefix apps/web
npm run typecheck --prefix apps/web
NEXT_TELEMETRY_DISABLED=1 npm run build --prefix apps/web
python scripts/verify_multi_item.py --output evals/reports/local/multi-item.json
python scripts/smoke.py
python scripts/web_http_smoke.py
python scripts/verify_recovery.py --output evals/reports/local/recovery.json
python scripts/verify_next.py --output evals/reports/local/native-gate
```

Synthetic examples are in `evals/fixtures/multi-item/`. `supplier-a.csv` totals
233.00 CNY with mixed line tax/discount terms; `supplier-b.csv` totals 247.00 CNY
with explicit zero-tax/zero-discount terms. They are examples, not real quotes.

The PostgreSQL gate explicitly selects all three new multi-item test modules;
selection is not evidence that PostgreSQL ran. Native browser cases are part of
`apps/web/e2e/workbench_e2e.py`; unit/type/build/HTTP results do not substitute
for browser execution. Current verification results are recorded separately in
`docs/evidence/stage17-local-verification.json` and the accompanying evidence
bundle. Existing baseline CI results are not counted as current passes.

## Executable isolated ERP gate, not yet run on actual ERPNext

The existing disposable ERPNext workflow now explicitly selects an additional
`multi-item-zero-tax-lost-receipt` scenario on **both SQLite and PostgreSQL**
business-database jobs. Its runner and evidence checker both require
`--include-multi-item`. A legacy eight-case report cannot satisfy the selected
nine-case gate. Legacy default fixtures and the separate pilot gate remain
unchanged; their earlier results cannot certify this ninth scenario.

The added case imports a two-row synthetic CSV for two seeded EA item IDs,
separately confirms fields and approves the complete snapshot, then dispatches
one draft. Its explicit zero-tax/zero-discount goods total is 41.80 CNY, with one
5.25 CNY freight charge, total 47.05 CNY. It exercises API/preview persistence
across restart, source-cell evidence, dropped-receipt reconciliation, independent
readback, idempotent reservation/worker replay and the exact one-POST count.

The independent MariaDB audit checks all two-line identities, commercial terms,
quantity/rate/goods/net components and freight. The evidence checker requires the
additional source hash, two-row provenance, exact nine-scenario set, one multi
record with two items, and preserved seven permission-denial probes. Missing,
duplicate or changed lines cannot be hidden by an unchanged grand total.

Only unit/mutation tests and an explicitly fake local HTTP ERP harness have run
for this expanded gate. The harness exercises the actual local API and restarted
workers but does not establish real ERP behavior. No actual ERPNext, MariaDB,
PostgreSQL or container gate has run for this increment. Adding multi-item Python
tests to the PostgreSQL selector also does **not** make those tests live ERP tests.

Before describing the multi-item adapter as live-verified, run a new authorized
exact-source isolated CI delivery and retain evidence for:

1. Use the existing pinned disposable ERPNext image and one-time synthetic
   company, dedicated restricted integration account, two distinct EA items,
   supplier and freight account. Retain CNY, unique operation-key metadata and
   database index checks, snapshot field, account-scope checks and negative PO
   permission probes. Do not use or grant production credentials.
2. Exercise the actual API on both disposable SQLite and PostgreSQL: create a
   two-line request, map/confirm a two-row CSV source, separately confirm quote
   fields, approve through a different identity and dispatch exactly one draft.
   Use explicit zero tax/discount, common tax mode and freight-once amounts.
3. Independently GET the saved Supplier Quotation and compare both item codes,
   units, quantities, rates, goods/net amounts, descriptions/commercial terms,
   pricing rules, discounts, tax/freight rows, total, company, currency, date,
   operation key, snapshot hash and docstatus. Capture bounded sanitized
   component evidence and the remote draft count, not a POST echo alone.
4. Run the actual multi-item native workbench against that isolated stack:
   select both source rows, inspect per-line evidence, confirm, approve, create,
   then request independent readback. A passing mock browser run does not fill
   this requirement. Verify known partial/duplicate/quantity errors cannot reach
   dispatch, and unsupported nonzero-tax/discount mappings produce zero POSTs.
5. Verify repeated reservation and concurrent workers still leave exactly one
   remote draft. Simulate a dropped response only inside the controlled sandbox,
   reconcile by GET, and check that absent/ambiguous results never cause a
   second POST. Safely test altered/missing/extra/duplicate lines only on these
   disposable synthetic records, preserving independent pre/post observations.
6. Check the pre-write authority gate after metadata reads, stale approval after
   any line change, pilot revocation, and paused/restored permanent operation
   holds. A recovery match must never release or replay the historical operation.
7. Retain separate browser, PostgreSQL, actual ERP readback and permission results
   with the exact source commit/tree and immutable source/record hashes. Fail the
   gate for missing/skipped scenarios. If standard descriptions or monetary
   persistence differ from this bounded mapping, fix the mapping and rerun the
   full gate before expanding claims or proposing deployment.

This plan does not authorize new accounts, credentials, real ERP writes,
production configuration changes or publication. It is the remaining isolated
integration gate to carry out under the appropriate user-approved workflow.


## Recorded local results

- Full Python regression: **2,662 passed**, 15 deliberately deselected by the
  non-browser/non-PostgreSQL command; 0 failed, 0 skipped within that selection.
- Frontend units: **82 passed**; TypeScript, production build and exported
  contract drift check passed. Actual HTTP smoke and all 5 frontend HTTP cases passed.
- Synthetic multi-item closed-loop acceptance and paired SQLite recovery passed.
  Offline graph protocol and the existing frozen 10-case development fixture passed;
  these are not real-model quality or cost results.
- Expanded ERP gate: **1,353 focused tests passed**, including the actual local
  API/restarted-worker nine-case harness with a fake HTTP ERP. This is a subset
  overlapping the full regression and is not nine real ERP executions.
- **32 native browser cases collect**, including seven new multi-item cases.
  Chromium launch was blocked by `CHROMIUM_UNIX_SOCKET_DENIED` before UI assertions;
  no browser pass or screenshots are claimed. Type/build/HTTP tests do not replace it.
- Actual PostgreSQL, containers, ERPNext/MariaDB, remote CI and independent security
  review were not run for this increment. No push, merge or deployment occurred.

Exact raw-record hashes and boundaries are in the [local verification index](evidence/stage17-local-verification.json).
Prior failed intermediate runs are superseded by this final complete regression;
no previously failing assertion was disabled or skipped to obtain the pass.
