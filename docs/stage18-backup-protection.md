# Stage 18: optional backup encryption and pinned provenance

This is a bounded local development increment built on
`a35f78713777016e8d76338391b18e0d558aa850` (tree
`ef4d442c06ba0a8dc14e37fc29ab02f6fcdc8e5f`). It has not been pushed, merged,
deployed or certified. Only invented, disposable data and ephemeral test keys
were used. No production keys, credentials, accounts or backups were created,
configured, exported or restored. Independent security review remains incomplete.

## Explicit protection policy

The operator CLI now requires `--protection` for backup, verification and restore.
The command fails if the flag is absent. There is no format autodetection,
automatic downgrade, optional signature verification or ignored key argument.

| Selected policy | Stored representation | Confidentiality | Provenance |
| --- | --- | --- | --- |
| `plain` | Existing V1 ZIP | None | None |
| `signed` | Compact JWS containing the full V1 ZIP | None | Pinned Ed25519 key |
| `encrypted` | Compact JWE containing the full V1 ZIP | A256GCM | None; symmetric key holders can create it |
| `signed-encrypted` | Compact JWE containing a compact JWS containing the full V1 ZIP | A256GCM | Pinned Ed25519 key |

Use `signed-encrypted` when both confidentiality and attributable provenance are
required. `signed` deliberately leaves all data readable. `encrypted` authenticates
the encrypted data, but cannot distinguish between holders of the symmetric key.
`plain` deliberately accepts unauthenticated data and should be confined to a
separately trusted offline process. Protected policies reject raw ZIPs; plain
rejects protected envelopes. Missing, unused, invalid or wrong keys cause failure.

The explicit API is `backup_protection.create_backup`, `verify_archive` and
`restore_archive`, each with a required keyword `mode`. The original
`backup.backup_database`, `verify_backup` and `restore_backup` remain V1-only
plaintext helpers for existing internal callers; they provide no cryptographic
protection. They never try to decode protected input. New operator workflows
should use the explicit policy API/CLI.

## Standard formats, narrow profile

This implementation uses jwcrypto 1.6.1 and pyca cryptography 50.0.2, installed
from their published PyPI releases. It does not implement cryptographic
primitives, nonce generation, an encryption construction, password derivation or
a new signature container. Dependencies are pinned in `requirements.txt` and the
runtime snapshot; this is not a cross-platform hash lock or vulnerability audit.

- [JWS, RFC 7515](https://www.rfc-editor.org/rfc/rfc7515.html): one compact
  signature, payload is the exact complete V1 ZIP bytes
- [JWE, RFC 7516](https://www.rfc-editor.org/rfc/rfc7516.html): one compact
  recipient, `alg=dir`, `enc=A256GCM`; the library generates the 96-bit IV
- [Fully specified Ed25519, RFC 9864](https://www.rfc-editor.org/rfc/rfc9864.html):
  exact `alg=Ed25519`, `kty=OKP`, `crv=Ed25519`; generic `EdDSA`, Ed448 and other
  algorithms are not accepted
- [JWK, RFC 7517](https://www.rfc-editor.org/rfc/rfc7517.html): explicit, minimal
  local keys; the library's public JWK thumbprint identifies the required signer

Headers must exactly match this versioned profile:

- JWS: `alg`, `typ=application/vnd.procureflow.paired-backup.v1+jws`,
  `cty=application/vnd.procureflow.paired-backup.v1+zip`, and the independently
  supplied public key's `kid` thumbprint
- JWE: `alg`, `enc`, `typ=application/vnd.procureflow.paired-backup.v1+jwe`, and
  `cty` equal to the JWS or ZIP media type demanded by the selected policy

The headers themselves are protected by JWS/JWE. Untrusted headers never select
keys, algorithms, a trust source or weaker behavior. JSON serialization,
multiple signatures/recipients, detached payloads, whitespace in the compact token, padded or
noncanonical base64url, duplicate JSON names, unexpected fields, `zip`, `crit`,
`b64`, embedded keys, `jku`, `x5u` and `x5c` are rejected. No network lookup is
performed. An encryption-only artifact cannot be passed as signed-and-encrypted,
or vice versa. The library algorithm allowlists are set explicitly in addition
to the exact header checks.

## Caller-managed keys and trust

Provision real keys only through the organization's authorized secret-management
workflow, outside this application. There is no key-generation, key-import
service, credential registration, rotation, password prompt or cloud key service
in this increment. Test fixtures create temporary synthetic material only.

Each key file must be a regular, single-link, owner-owned file with no group or
world permissions, at most 4,096 bytes. Final symlinks, FIFOs, directories,
world-readable files, hard links and files changed while being read are refused.
Protect the containing directories too. Keep private/symmetric material outside
source repositories, document storage, backup output and ordinary file sharing.
Do not paste keys into arguments, environment variables, logs or tickets.

The file contains UTF-8 minimal JWK JSON, with exactly these fields:

- Encryption/decryption: `kty` equal to `oct`, `k` equal to a canonical unpadded
  base64url representation of 32 bytes of externally provisioned random material
- Signing: `kty=OKP`, `crv=Ed25519`, `x` and `d`, each canonical unpadded
  base64url for 32 bytes. Private and public components must match
- Verification: `kty=OKP`, `crv=Ed25519`, `x` only. A private JWK is rejected
  in place of the public pin

The strict profile intentionally rejects even optional JWK fields such as
`alg`, `use`, `key_ops` and `kid`. Operator tooling must export the minimal
profile. No example operational key or key-generation command is shipped.

Obtain the verification key through a separately authenticated channel. A public
key or fingerprint delivered alongside an untrusted archive is not a trust
anchor. Matching the pinned key proves only that the corresponding private key
signed these bytes; it does not prove that the source data was accurate, the
signer was honest, or their host was uncompromised.

The operator owns provisioning permission, custody, access control, distribution,
rotation/revocation, retaining old decryption keys and trustworthy historical
verification keys, and recovery drills. Encryption and signing keys should be
separate and have different access policies. Losing the decryption key prevents
recovery. Compromised keys require a separately authorized response. This tool
does not fetch revoked-key lists, enforce signature timestamps, reject an older
valid backup automatically or prevent rollback. Select and audit the intended
backup ID/time/source revision independently.

## Operator examples

These commands describe a separately authorized operator procedure; they were
not run against a real source. Pause the source and stop non-cooperating writers
as described in [stage 15](stage15-recovery.md). Prepare all referenced files
through your approved process before invoking these commands.

```sh
python -m procureflow.backup_cli backup \
  --protection signed-encrypted \
  --database-url-file /secure/source-database-url \
  --documents /srv/procureflow/documents \
  --output /secure/backups/recovery-point.pfb \
  --source-revision <git-sha> \
  --encryption-key-file /secure/backup-encryption.jwk \
  --signing-key-file /secure/backup-signing-private.jwk

python -m procureflow.backup_cli verify /secure/backups/recovery-point.pfb \
  --protection signed-encrypted \
  --encryption-key-file /secure/backup-encryption.jwk \
  --verification-key-file /secure/trusted-backup-signing-public.jwk

python -m procureflow.backup_cli restore /secure/backups/recovery-point.pfb \
  --data-dir /srv/recovery/new-drill \
  --protection signed-encrypted \
  --encryption-key-file /secure/backup-encryption.jwk \
  --verification-key-file /secure/trusted-backup-signing-public.jwk
```

For isolated PostgreSQL recovery, the existing explicit owner-only
`--postgres-url-file` still creates only a new random schema. No ambient database
URL or ERP/model credentials are consulted. Do not run a restore drill against a
production database or source directory. All commands leave the source paused.
Key material and library exception details are never included in normal CLI
results/errors. A successful verification reports the selected policy and,
when verified, the public signer thumbprint.

## Verification before side effects

Creation retains the existing exclusive maintenance fence while snapshotting,
validating, protecting, checking and publishing the bundle. Protected creation
stages the raw ZIP only in memory, self-verifies its new JOSE artifact and writes
only protected bytes to the owner-only temporary output. Publication remains
atomic and exclusive; an existing or concurrently created output is never
replaced. Failed creation removes its temporary output and leaves the source
paused. This is not secure erasure: Python memory, OS swap/core dumps and a
compromised host remain outside the protection boundary.

Restore reads a bounded, stable file, authenticates JWE when required, verifies
JWS against the pinned key when required, then runs every original strict ZIP,
manifest, source-fingerprint, schema, table, relational-constraint, hash and
source-file-binding check. Only after all checks succeed may it create the new
restore directory, connect to the explicit PostgreSQL destination or create a
schema. Bad/truncated/tampered input, wrong keys or mismatched policy cannot
silently produce a partial plaintext restore.

The original fresh-target contract, incomplete markers after a later operational
failure, RECOVERY pause, pilot-only requirement, credential revocation, generation
invalidation and permanent holds remain intact. Existing uncertain operations,
idempotency keys, attempt counters and outbox state stay unchanged and cannot be
replayed, including after reviewed resume. The restore report and the new audit
receipt record the verified protection policy/public signer thumbprint. They do
not contain private or symmetric key material.

## Resource and compatibility limits

Protected profiles accept at most **32 MiB of raw V1 ZIP**, less than the legacy
V1 256 MiB ceiling, because JOSE and this implementation hold several copies in
memory. Fixed encoded JWS/JWE bounds are checked before parser/decryption calls;
headers and keys have separate small limits. Plain mode keeps the existing V1
limits. No compression or streaming is provided. This is a bounded pilot-scale
implementation, not a large-database backup system. Maximum-size synthetic
measurements and their environment belong in the evidence, not a production
capacity promise. On this local Python 3.12/Linux environment, a codec-only
synthetic 32 MiB input produced a 59,653,000-byte signed-encrypted envelope,
completed encode/self-check/decode in 10.073 seconds and reached 587,468 KiB peak
RSS. That measurement excludes database export and ZIP/schema validation; it is
not a full-restore memory ceiling, benchmark across hosts or service-level promise.

This is a new outer protection profile around the original V1 contract, **not a
source-version migration**. A V1 backup still requires exactly matching trusted
application source and schema. Earlier `a35f787`/stage-15/16/17 archives therefore
cannot be restored by this changed source just by adding `--protection plain`.
Use the matching trusted version for those archives; there is no compatibility
flag, source-fingerprint override or migration bypass. The operator CLI's new
required flag is an intentional breaking change for old scripts.

## Synthetic validation and remaining gates

```sh
python scripts/test.py -q tests/test_backup_protection.py tests/test_backup_recovery.py -m 'not postgres'
python scripts/verify_recovery.py --protection signed-encrypted --output evals/reports/local/recovery-protected.json
python scripts/verify_recovery.py --protection signed --output evals/reports/local/recovery-signed.json
python scripts/verify_recovery.py --protection encrypted --output evals/reports/local/recovery-encrypted.json
python scripts/verify_recovery.py --protection plain --output evals/reports/local/recovery-plain.json
```

The acceptance drill synthesizes its complete dataset and ephemeral keys in
memory/temp directories, restores a fresh SQLite target, checks preserved
evidence/revoked credentials/paused workers, reviews resume and verifies held
ERP work remains read-only. It does not read production keys or call live ERP,
real models, a key server or other external services.

Local results: the existing offline core suite passed 2,662 cases; the focused
backup suite passed 330 cases, comprising 278 new protection cases and 52 legacy
backup cases. The 52 legacy cases overlap. The union of their JUnit test IDs
exactly matches all 2,940 cases selected from the final source; 15 environment-
dependent cases were deselected. There were no failures, errors or skipped cases
in either selected suite. All four synthetic recovery profiles, API contract
drift, compilation and dependency-consistency checks passed. The existing
Starlette/AnyIO deprecation warning remains.

Exact commands, counts, source identity and results are captured in
[the local evidence index](evidence/stage18-local-verification.json) and the
delivery's original logs. Historical CI for `a35f787` is baseline evidence only. No current-change
remote CI, live PostgreSQL, Docker/native-browser gate, production restore or
independent security certification is established by local synthetic tests.

Official implementation references:
[jwcrypto JWS](https://jwcrypto.readthedocs.io/en/stable/jws.html),
[jwcrypto JWE](https://jwcrypto.readthedocs.io/en/stable/jwe.html),
[jwcrypto 1.6.1 releases](https://github.com/latchset/jwcrypto/releases/tag/v1.6.1),
[pyca cryptography](https://cryptography.io/en/stable/).
