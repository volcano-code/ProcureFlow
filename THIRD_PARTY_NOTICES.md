# Third-party dependencies and fixture provenance

This repository contains newly written application code and synthetic sample quotations. It does not vendor framework source, browser binaries, model weights or font files.

Python dependency versions are recorded in `services/api/requirements.txt` and the tested-environment transitive snapshot `requirements.lock`. Test dependencies are in `requirements-dev.txt`. JavaScript dependencies are declared in `apps/web/package.json`; a resolved npm lockfile is not yet available. Each dependency remains subject to its own upstream license. The MIT license in this repository applies to the newly written code, not to third-party components.

`evals/fixtures/` and `apps/demo/samples/` contain invented suppliers SUP-A/B/C, invented SKU STAND-01, and invented prices. They are test data, not real quotations, price research, or recommendations to buy. The PDF was generated programmatically; the XLSX was generated using an artifact spreadsheet library. No real company or personal procurement data is included. Files may be reused with the repository's license.

The local workbench uses system fonts. No font files are distributed. Official framework and protocol references used in design review are listed in `docs/assessment.md`.

## Optional PostgreSQL driver

psycopg and psycopg-binary: LGPL-3.0-or-later. Dependency: psycopg[binary]==3.3.6. Retain package license metadata when distributing built images. See https://www.psycopg.org/psycopg3/ and https://pypi.org/project/psycopg/.

## Read-only graph runtime

LangGraph 1.2.12 and LangSmith SDK 0.14.2: MIT. Official sources: https://github.com/langchain-ai/langgraph and https://github.com/langchain-ai/langsmith-sdk. Both are installed from PyPI, not vendored. LangSmith SDK is used only to explicitly disable tracing around local graph execution; no hosted LangSmith service is enabled. The runtime dependency snapshot includes transitive packages with their own upstream licenses. This is not a comprehensive license/security audit.

## Optional backup protection

JWCrypto 1.6.1: LGPL-3.0-or-later. pyca cryptography 50.0.2: Apache-2.0 OR BSD-3-Clause.
Both are installed from published PyPI packages, not vendored. Retain their package
license metadata when distributing built images. New transitive snapshot entries
include cffi 2.1.1 (MIT-0) and pycparser 3.1 (BSD-3-Clause). Sources:
https://github.com/latchset/jwcrypto and https://github.com/pyca/cryptography.
This is dependency attribution, not an independent security or license audit.
