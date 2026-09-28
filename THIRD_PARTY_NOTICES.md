# Third-party dependencies and fixture provenance

This repository contains newly written application code and synthetic sample quotations. It does not vendor framework source, browser binaries, model weights or font files.

Python dependency versions are recorded in `services/api/requirements.txt` and the tested-environment transitive snapshot `requirements.lock`. Test dependencies are in `requirements-dev.txt`. JavaScript dependencies are declared in `apps/web/package.json`; a resolved npm lockfile is not yet available. Each dependency remains subject to its own upstream license. The MIT license in this repository applies to the newly written code, not to third-party components.

`evals/fixtures/` and `apps/demo/samples/` contain invented suppliers SUP-A/B/C, invented SKU STAND-01, and invented prices. They are test data, not real quotations, price research, or recommendations to buy. The PDF was generated programmatically; the XLSX was generated using an artifact spreadsheet library. No real company or personal procurement data is included. Files may be reused with the repository's license.

The local workbench uses system fonts. No font files are distributed. Official framework and protocol references used in design review are listed in `docs/assessment.md`.
