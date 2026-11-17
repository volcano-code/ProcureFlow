# API contracts

`openapi.json` contains every schema-visible FastAPI endpoint, including readiness
and independent ERP operation verification. `quotation.schema.json` and
`request.schema.json` are the matching Pydantic model schemas. Static assets,
the workbench page and FastAPI's documentation routes are intentionally excluded.

From the repository root, with `services/api/requirements-dev.txt` installed:

```bash
python scripts/export_contracts.py          # regenerate all three files
python scripts/export_contracts.py --check  # read-only drift gate
```

Review and commit regenerated contracts alongside API changes. The CI core gate
runs `--check`; it fails on missing files or any byte-level difference (including
formatting), without rewriting the committed files. Exit codes are 0 for success,
1 for drift, and 2 for a generation/check error. Generation errors never count as
a passing check. `--output-dir PATH` supports an alternate output/check directory.

Generation runs in a fresh Python process with disposable SQLite and mock ERP
storage. It discards ambient `PF_*`, `ERP_*` and `LLM_*` configuration, uses a fixed
hash seed, and does not invoke endpoints, startup lifespans or external services.
It does not open the user's database or use ERP/model credentials. The pinned
FastAPI and Pydantic dependencies, UTF-8 encoding and fixed newline format are
part of reproducibility. Regenerating schemas does not change runtime validation,
authorization, endpoint behavior or approval requirements.
