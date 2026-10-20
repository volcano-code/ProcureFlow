# Advisory candidate migration and ERP boundary

The remediation candidate flow adds application migration `f192c4ad8601` after
`e731bb62c905`. It does not change ERPNext schema or grant any extra ERP role.
Apply the application migration before starting a private/PostgreSQL installation.

Importing a candidate creates an unconfirmed quote. Existing explicit confirmation,
independent snapshot approval, operation ledger and Worker readback remain required.
Candidate tools have no ERP endpoint, credentials or execute/approve capability.

The disposable ERP gates must be rerun at the final remediation commit rather than
borrowing historical successes. They remain synthetic system tests, not tests of
model extraction quality or company purchasing. Do not enable candidate model calls
or expose provider secrets in the automatic ERP sandbox workflows.
