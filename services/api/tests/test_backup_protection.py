"""Synthetic functional/adversarial regressions, not a security review or certification.

Keys are freshly generated disposable fixtures. No production source, credentials,
network service, or persistent key store is used by this suite.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from jwcrypto import jwk, jwe, jws
import pytest
from sqlalchemy import select

import procureflow.backup as backup
import procureflow.backup_protection as protection
from procureflow.backup import BackupError
from procureflow.backup_cli import main
from procureflow.db import Base, Database
from procureflow.errors import DomainError
from procureflow.maintenance import recovery_report, resume_writes
from test_backup_recovery import _rewrite, bundle, source  # noqa: F401: shared synthetic fixtures


MODES = ("plain", "signed", "encrypted", "signed-encrypted")
ZIP_TYPE = "application/vnd.procureflow.paired-backup.v1+zip"
JWS_TYPE = "application/vnd.procureflow.paired-backup.v1+jws"
JWE_TYPE = "application/vnd.procureflow.paired-backup.v1+jwe"


def _json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _unb64(value):
    return base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))


@pytest.fixture
def keys():
    signing = jwk.JWK.generate(kty="OKP", crv="Ed25519")
    stranger = jwk.JWK.generate(kty="OKP", crv="Ed25519")
    return {
        "encryption": jwk.JWK.generate(kty="oct", size=256).export().encode(),
        "wrong_encryption": jwk.JWK.generate(kty="oct", size=256).export().encode(),
        "signing": signing.export_private().encode(),
        "verification": signing.export_public().encode(),
        "stranger_signing": stranger.export_private().encode(),
        "stranger_verification": stranger.export_public().encode(),
    }


def _write_options(mode, keys):
    return {"mode": mode,
            **({"encryption_key": keys["encryption"]} if "encrypted" in mode else {}),
            **({"signing_key": keys["signing"]} if "signed" in mode else {})}


def _read_options(mode, keys):
    return {"mode": mode,
            **({"encryption_key": keys["encryption"]} if "encrypted" in mode else {}),
            **({"verification_key": keys["verification"]} if "signed" in mode else {})}


def _wrap(raw, mode, keys):
    """Build valid envelopes independently of the application's encoder."""
    if "signed" in mode:
        key = jwk.JWK.from_json(keys["signing"])
        token = jws.JWS(raw)
        token.add_signature(key, alg="Ed25519", protected={
            "alg": "Ed25519", "typ": JWS_TYPE, "cty": ZIP_TYPE, "kid": key.thumbprint()})
        raw = token.serialize(compact=True).encode()
    if "encrypted" in mode:
        token = jwe.JWE(raw, protected={"alg": "dir", "enc": "A256GCM", "typ": JWE_TYPE,
                                      "cty": JWS_TYPE if "signed" in mode else ZIP_TYPE})
        token.add_recipient(jwk.JWK.from_json(keys["encryption"]))
        raw = token.serialize(compact=True).encode()
    return raw


def _archive(tmp_path, raw, name="wrapped.pfb"):
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _rows(db, name):
    table = Base.metadata.tables[name]
    with db.transaction() as session:
        return [dict(row) for row in session.execute(
            select(table).order_by(*table.primary_key.columns)).mappings()]


def _assert_rejected_before_restore(path, tmp_path, monkeypatch, **options):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated/invalid input reached restore or a persistent database connection")

    create_engine = backup.create_engine
    def transient_validation_only(url, *args, **kwargs):
        # V1 checks logical constraints in a disposable in-memory SQLite DB.
        # No persistent target or PostgreSQL connection may be reached.
        if url != "sqlite:///:memory:":
            forbidden()
        return create_engine(url, *args, **kwargs)

    monkeypatch.setattr(protection, "_restore_verified_backup", forbidden)
    monkeypatch.setattr(backup, "create_engine", transient_validation_only)
    target = tmp_path / "must-not-exist"
    with pytest.raises(BackupError):
        protection.restore_archive(path, target, postgres_url="postgresql://invalid.invalid/synthetic_test", **options)
    assert not target.exists()


@pytest.mark.parametrize("mode", MODES)
def test_roundtrip_preserves_evidence_credentials_and_held_ledger(source, tmp_path, keys, mode):
    db, documents, blob = source
    path = tmp_path / "protected.pfb"
    result = protection.create_backup(db, documents, path, source_revision="1234567",
                                      **_write_options(mode, keys))
    original = protection.verify_archive(path, **_read_options(mode, keys))
    receipt = {"mode": mode, "provenance_verified": "signed" in mode}
    if "signed" in mode:
        receipt["signer_thumbprint"] = jwk.JWK.from_json(keys["verification"]).thumbprint()
    assert result["backup_protection"] == original.protection == receipt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert recovery_report(db)["state"] == "PAUSED"
    target = tmp_path / "restored"
    restored = protection.restore_archive(path, target, **_read_options(mode, keys))
    assert restored.report()["backup_protection"] == receipt
    assert (target / "documents" / "doc-fixture.txt").read_bytes() == blob
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert not (target / ".restore-incomplete").exists()
    restored_db = Database(f"sqlite:///{target / 'procureflow.sqlite3'}")
    try:
        for name in ("procurement_requests", "documents", "quotes", "quote_versions", "approvals",
                     "external_operations", "outbox", "evaluations", "policy_versions", "advice_runs",
                     "table_imports"):
            assert _rows(restored_db, name) == original.tables[name], name
        events = _rows(restored_db, "audit_events")
        assert events[:-1] == original.tables["audit_events"]
        assert events[-1]["type"] == "BACKUP_RESTORED"
        assert events[-1]["payload"]["backup_protection"] == receipt
        for name in ("pilot_sessions", "pilot_invites"):
            assert all(row["revoked_at"] for row in _rows(restored_db, name))
        for name in ("pilot_tenants", "pilot_memberships"):
            assert [row["generation"] for row in _rows(restored_db, name)] == [
                row["generation"] + 1 for row in original.tables[name]]
        report = recovery_report(restored_db)
        assert report["state"] == "RECOVERY"
        assert report["required_auth_mode"] == "pilot"
        assert report["automatic_replay_enabled"] is False
        assert sum(bool(row["hold_restore_id"]) for row in report["operations"]) == 5
        with pytest.raises(DomainError, match="Writes are paused"):
            with restored_db.transaction(write=True):
                pass
        with pytest.raises(DomainError) as caught:
            resume_writes(restored_db, generation=report["generation"], ledger_sha256=report["ledger_sha256"])
        assert caught.value.code == "RECOVERY_REVIEW_REQUIRED"
        ledger = {name: _rows(restored_db, name) for name in ("external_operations", "outbox", "recovery_holds")}
        resumed = resume_writes(restored_db, generation=report["generation"], ledger_sha256=report["ledger_sha256"],
                                restore_id=restored.backup_id, acknowledge_credentials=True,
                                acknowledge_reconciliation=True)
        assert resumed["state"] == "ACTIVE" and resumed["required_auth_mode"] == "pilot"
        assert resumed["operations"] == report["operations"]
        for name, rows in ledger.items():
            assert _rows(restored_db, name) == rows, name
    finally:
        restored_db.engine.dispose()


def test_mode_is_a_required_keyword_before_any_source_or_target_access(tmp_path):
    with pytest.raises(TypeError, match="mode"):
        protection.create_backup(None, tmp_path / "missing", tmp_path / "output")
    with pytest.raises(TypeError, match="mode"):
        protection.verify_archive(tmp_path / "missing")
    with pytest.raises(TypeError, match="mode"):
        protection.restore_archive(tmp_path / "missing", tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("mode", [None, True, "", "auto", "SIGNED", "Ed25519", "signed_encrypted"])
def test_invalid_policy_rejected_before_source_access(tmp_path, mode):
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_POLICY_REQUIRED"):
        protection.create_backup(None, tmp_path / "missing", tmp_path / "output", mode=mode)


@pytest.mark.parametrize("mode,provide_encryption,provide_authority", [
    (mode, encrypted, signed) for mode in MODES
    for encrypted, signed in ((False, False), (True, False), (False, True), (True, True))
    if (encrypted, signed) != ("encrypted" in mode, "signed" in mode)
])
def test_missing_or_extra_keys_rejected_before_io(tmp_path, keys, mode, provide_encryption, provide_authority):
    encryption = keys["encryption"] if provide_encryption else None
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEYS_MISMATCH"):
        protection.create_backup(None, tmp_path / "missing", tmp_path / "output", mode=mode,
                                  encryption_key=encryption,
                                  signing_key=keys["signing"] if provide_authority else None)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEYS_MISMATCH"):
        protection.verify_archive(tmp_path / "missing", mode=mode, encryption_key=encryption,
                                   verification_key=keys["verification"] if provide_authority else None)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("mode", MODES)
def test_existing_output_and_restore_target_never_replaced(source, tmp_path, keys, mode):
    path = tmp_path / "existing.pfb"
    path.write_bytes(b"existing precious output")
    with pytest.raises(BackupError, match="BACKUP_OUTPUT_EXISTS"):
        protection.create_backup(source[0], source[1], path, **_write_options(mode, keys))
    assert path.read_bytes() == b"existing precious output"
    path.unlink()
    protection.create_backup(source[0], source[1], path, **_write_options(mode, keys))
    target = tmp_path / "existing-target"
    target.mkdir()
    (target / "precious").write_bytes(b"keep")
    with pytest.raises(BackupError, match="RESTORE_TARGET_EXISTS"):
        protection.restore_archive(path, target, **_read_options(mode, keys))
    assert list(target.iterdir()) == [target / "precious"]
    assert (target / "precious").read_bytes() == b"keep"


@pytest.mark.parametrize("mode", MODES[1:])
def test_protection_does_not_bypass_source_pause(source, tmp_path, keys, mode):
    db, documents, _ = source
    report = recovery_report(db)
    resume_writes(db, generation=report["generation"], ledger_sha256=report["ledger_sha256"])
    output = tmp_path / "not-paused.pfb"
    with pytest.raises(DomainError) as caught:
        protection.create_backup(db, documents, output, **_write_options(mode, keys))
    assert caught.value.code == "MAINTENANCE_PAUSE_REQUIRED"
    assert not output.exists()


@pytest.mark.parametrize("actual,requested", [(actual, requested) for actual in MODES for requested in MODES
                                               if actual != requested])
def test_no_mode_autodetection_or_downgrade(bundle, tmp_path, keys, monkeypatch, actual, requested):
    path = _archive(tmp_path, _wrap(bundle.read_bytes(), actual, keys))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(requested, keys))


@pytest.mark.parametrize("mode", ["encrypted", "signed-encrypted"])
def test_wrong_decryption_key_rejected_before_target_or_postgres(bundle, tmp_path, keys, monkeypatch, mode):
    path = _archive(tmp_path, _wrap(bundle.read_bytes(), mode, keys))
    options = _read_options(mode, keys)
    options["encryption_key"] = keys["wrong_encryption"]
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **options)


@pytest.mark.parametrize("mode", ["signed", "signed-encrypted"])
def test_untrusted_signer_is_not_accepted_from_archive(bundle, tmp_path, keys, monkeypatch, mode):
    stranger = {**keys, "signing": keys["stranger_signing"]}
    path = _archive(tmp_path, _wrap(bundle.read_bytes(), mode, stranger))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("mode", ["signed", "signed-encrypted"])
def test_spoofed_trusted_kid_cannot_bypass_signature_check(bundle, tmp_path, keys, monkeypatch, mode):
    token = jws.JWS(bundle.read_bytes())
    token.add_signature(jwk.JWK.from_json(keys["stranger_signing"]), alg="Ed25519", protected={
        "alg": "Ed25519", "typ": JWS_TYPE, "cty": ZIP_TYPE,
        "kid": jwk.JWK.from_json(keys["verification"]).thumbprint()})
    raw = token.serialize(compact=True).encode()
    if mode == "signed-encrypted":
        encrypted = jwe.JWE(raw, protected={"alg": "dir", "enc": "A256GCM", "typ": JWE_TYPE, "cty": JWS_TYPE})
        encrypted.add_recipient(jwk.JWK.from_json(keys["encryption"]))
        raw = encrypted.serialize(compact=True).encode()
    path = _archive(tmp_path, raw)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_AUTHENTICATION_FAILED"):
        protection.verify_archive(path, **_read_options(mode, keys))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("mode,index", [("signed", 0), ("signed", 1), ("signed", 2),
                                        ("encrypted", 0), ("encrypted", 2), ("encrypted", 3), ("encrypted", 4),
                                        ("signed-encrypted", 0), ("signed-encrypted", 2),
                                        ("signed-encrypted", 3), ("signed-encrypted", 4)])
def test_tampered_crypto_segments_fail_before_restore(bundle, tmp_path, keys, monkeypatch, mode, index):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    segment = parts[index]
    parts[index] = (b"A" if segment[:1] != b"A" else b"B") + segment[1:]
    path = _archive(tmp_path, b".".join(parts))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("mode", MODES[1:])
@pytest.mark.parametrize("mutation", ["empty", "truncated", "trailing", "prefix", "json", "non-ascii", "extra-part"])
def test_malformed_envelopes_rejected(bundle, tmp_path, keys, monkeypatch, mode, mutation):
    raw = _wrap(bundle.read_bytes(), mode, keys)
    variants = {"empty": b"", "truncated": raw[:-7], "trailing": raw + b"\n", "prefix": b"prefix " + raw,
                "json": _json({"payload": raw.decode()}), "non-ascii": raw + b"\xff", "extra-part": raw + b".AA"}
    path = _archive(tmp_path, variants[mutation])
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("mode", ["signed", "encrypted"])
@pytest.mark.parametrize("field,value", [
    ("alg", "none"), ("alg", "EdDSA"), ("alg", "HS256"), ("alg", "RSA-OAEP"),
    ("enc", "A128GCM"), ("zip", "DEF"), ("jwk", {"kty": "oct", "k": "AAAA"}),
    ("jku", "https://invalid.invalid/untrusted-jwks"), ("x5u", "https://invalid.invalid/certificate"),
    ("x5c", ["untrusted"]), ("crit", []), ("crit", ["b64"]), ("b64", False), ("b64", True),
    ("typ", "JWT"), ("cty", "application/json"), ("kid", "untrusted"), ("unknown", "value"),
])
def test_exact_header_allowlist_rejected_before_crypto(bundle, tmp_path, keys, mode, field, value):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    header = json.loads(_unb64(parts[0]))
    header[field] = value
    parts[0] = _b64(_json(header))
    path = _archive(tmp_path, b".".join(parts))
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_HEADER_INVALID"):
        protection.verify_archive(path, **_read_options(mode, keys))


@pytest.mark.parametrize("mode", ["signed", "encrypted"])
@pytest.mark.parametrize("header", [b'{"alg":"none","alg":"Ed25519"}', b'[]', b'null', b'"header"',
                                      b'{"alg":NaN}', b'\xff', b'{', b'{"alg":"dir",}'])
def test_duplicate_or_malformed_header_json_rejected(bundle, tmp_path, keys, mode, header):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    parts[0] = _b64(header)
    path = _archive(tmp_path, b".".join(parts))
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_(INVALID_JSON|HEADER_INVALID)"):
        protection.verify_archive(path, **_read_options(mode, keys))


@pytest.mark.parametrize("inner", ["unsigned-zip", "unknown-header", "duplicate-header", "bad-signature"])
def test_valid_outer_encryption_does_not_bypass_inner_signature_policy(bundle, tmp_path, keys, monkeypatch, inner):
    raw = _wrap(bundle.read_bytes(), "signed", keys)
    parts = raw.split(b".")
    if inner == "unsigned-zip":
        raw = bundle.read_bytes()
    elif inner == "unknown-header":
        parts[0] = _b64(_json({**json.loads(_unb64(parts[0])), "jku": "https://invalid.invalid/keys"}))
        raw = b".".join(parts)
    elif inner == "duplicate-header":
        parts[0] = _b64(b'{"alg":"Ed25519","alg":"none"}')
        raw = b".".join(parts)
    else:
        parts[2] = _b64(b"\x00" * 64)
        raw = b".".join(parts)
    outer = jwe.JWE(raw, protected={"alg": "dir", "enc": "A256GCM", "typ": JWE_TYPE, "cty": JWS_TYPE})
    outer.add_recipient(jwk.JWK.from_json(keys["encryption"]))
    path = _archive(tmp_path, outer.serialize(compact=True).encode())
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options("signed-encrypted", keys))


@pytest.mark.parametrize("mode,index", [("signed", 0), ("signed", 1), ("signed", 2),
                                        ("encrypted", 0), ("encrypted", 2), ("encrypted", 3), ("encrypted", 4)])
def test_padded_base64_is_not_accepted(bundle, tmp_path, keys, mode, index):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    parts[index] += b"="
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_ENCODING_INVALID"):
        protection.verify_archive(_archive(tmp_path, b".".join(parts)), **_read_options(mode, keys))


def _noncanonical_alias(segment):
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(segment) % 4 in (2, 3), "Fixture needs unused final base64 bits"
    alias = segment[:-1] + bytes([alphabet[alphabet.index(segment[-1]) | 1]])
    assert alias != segment and _unb64(alias) == _unb64(segment)
    return alias


@pytest.mark.parametrize("mode,index", [("signed", 2), ("encrypted", 4)])
def test_noncanonical_signature_or_tag_encoding_rejected(bundle, tmp_path, keys, mode, index):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    parts[index] = _noncanonical_alias(parts[index])
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_ENCODING_INVALID"):
        protection.verify_archive(_archive(tmp_path, b".".join(parts)), **_read_options(mode, keys))


@pytest.mark.parametrize("mode", ["encrypted", "signed-encrypted"])
def test_noncanonical_ciphertext_encoding_rejected(source, tmp_path, keys, mode):
    # Revision length changes the valid ZIP size; no invalid ZIP padding is used.
    for length in range(7, 11):
        path = tmp_path / f"canonical-{length}.pfb"
        protection.create_backup(source[0], source[1], path, source_revision="a" * length,
                                  **_write_options(mode, keys))
        parts = path.read_bytes().split(b".")
        if len(parts[3]) % 4 in (2, 3):
            break
    else:
        pytest.fail("Could not construct ciphertext with unused base64 bits")
    protection.verify_archive(path, **_read_options(mode, keys))
    parts[3] = _noncanonical_alias(parts[3])
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_ENCODING_INVALID"):
        protection.verify_archive(_archive(tmp_path, b".".join(parts)), **_read_options(mode, keys))


@pytest.mark.parametrize("mode", ["encrypted", "signed-encrypted"])
@pytest.mark.parametrize("index,value", [(1, b"AA"), (2, b""), (2, b"AA"), (3, b""), (4, b""), (4, b"AA")])
def test_direct_encryption_field_lengths_rejected(bundle, tmp_path, keys, mode, index, value):
    parts = _wrap(bundle.read_bytes(), mode, keys).split(b".")
    parts[index] = value
    with pytest.raises(BackupError):
        protection.verify_archive(_archive(tmp_path, b".".join(parts)), **_read_options(mode, keys))


@pytest.mark.parametrize("mode", MODES[1:])
@pytest.mark.parametrize("mutation,code", [
    (lambda m: m.update(version=99), "BACKUP_UNSUPPORTED_VERSION"),
    (lambda m: m["source"].update(fingerprint="0" * 64), "BACKUP_SOURCE_VERSION_MISMATCH"),
    (lambda m: m.update(schema_heads=["unknown"]), "BACKUP_SCHEMA_VERSION_MISMATCH"),
    (lambda m: m.update(schema={}), "BACKUP_SCHEMA_VERSION_MISMATCH"),
    (lambda m: m.update(schema_sha256="0" * 64), "BACKUP_SCHEMA_VERSION_MISMATCH"),
    (lambda m: m["database"].update(sha256="0" * 64), "BACKUP_DATABASE_HASH_MISMATCH"),
    (lambda m: m["documents"]["doc-fixture.txt"].update(sha256="0" * 64), "BACKUP_DOCUMENT_HASH_MISMATCH"),
    (lambda m: m["documents"]["doc-fixture.txt"].update(bytes=1), "BACKUP_DOCUMENT_HASH_MISMATCH"),
    (lambda m: m["limits"].update(archive_bytes=2**60), "BACKUP_LIMITS_MISMATCH"),
])
def test_valid_crypto_does_not_override_payload_bindings(bundle, tmp_path, keys, monkeypatch, mode, mutation, code):
    altered = _rewrite(bundle, tmp_path / "altered.zip", mutate_manifest=mutation)
    path = _archive(tmp_path, _wrap(altered.read_bytes(), mode, keys))
    with pytest.raises(BackupError, match=code):
        protection.verify_archive(path, **_read_options(mode, keys))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("mode", MODES[1:])
def test_valid_crypto_does_not_make_hostile_logical_rows_safe(bundle, tmp_path, keys, monkeypatch, mode):
    altered = _rewrite(bundle, tmp_path / "altered.zip",
                       mutate_database=lambda data: data["tables"]["documents"][0].update(storage_key="../escape"))
    path = _archive(tmp_path, _wrap(altered.read_bytes(), mode, keys))
    with pytest.raises(BackupError, match="BACKUP_UNSAFE_STORAGE_KEY"):
        protection.verify_archive(path, **_read_options(mode, keys))
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


@pytest.mark.parametrize("kind", ["encryption", "signing", "verification"])
@pytest.mark.parametrize("mutation", ["empty", "oversize", "text", "duplicate", "extra", "missing", "wrong-type", "non-json"])
def test_minimal_jwk_shape_is_required(tmp_path, keys, kind, mutation):
    key = keys[kind]
    parsed = json.loads(key)
    first = next(iter(parsed))
    candidates = {
        "empty": b"", "oversize": b" " * 4097, "text": key.decode(),
        "duplicate": key[:-1] + b"," + _json(first) + b":" + _json(parsed[first]) + b"}",
        "extra": _json({**parsed, "kid": "no-key-discovery"}),
        "missing": _json({name: value for name, value in parsed.items() if name != first}),
        "wrong-type": _json({**parsed, first: 123}), "non-json": b"not-a-jwk",
    }
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        if kind == "encryption":
            protection.verify_archive(tmp_path / "missing", mode="encrypted", encryption_key=candidates[mutation])
        elif kind == "signing":
            protection.create_backup(None, tmp_path / "missing", tmp_path / "output", mode="signed",
                                      signing_key=candidates[mutation])
        else:
            protection.verify_archive(tmp_path / "missing", mode="signed", verification_key=candidates[mutation])


@pytest.mark.parametrize("kind,field,value", [
    ("encryption", "kty", "RSA"), ("encryption", "k", _b64(b"x" * 16).decode()),
    ("encryption", "k", _b64(b"x" * 31).decode()), ("encryption", "k", _b64(b"x" * 33).decode()),
    ("encryption", "k", "é"), ("encryption", "k", "A"), ("encryption", "k", "NaN"),
    ("signing", "crv", "Ed448"), ("signing", "kty", "EC"), ("signing", "d", _b64(b"x" * 31).decode()),
    ("verification", "crv", "X25519"), ("verification", "x", _b64(b"x" * 33).decode()),
])
def test_wrong_key_algorithms_lengths_and_encodings_rejected(keys, kind, field, value):
    parsed = json.loads(keys[kind])
    parsed[field] = value
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        protection._load_key(_json(parsed), kind)


def test_jwk_private_public_separation_and_pair_consistency(keys):
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        protection._load_key(keys["signing"], "verification")
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        protection._load_key(keys["verification"], "signing")
    mismatched = json.loads(keys["signing"])
    mismatched["x"] = json.loads(keys["stranger_verification"])["x"]
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        protection._load_key(_json(mismatched), "signing")


@pytest.mark.parametrize("kind,field", [("encryption", "k"), ("signing", "d"), ("signing", "x"), ("verification", "x")])
def test_jwk_noncanonical_base64_rejected(keys, kind, field):
    parsed = json.loads(keys[kind])
    parsed[field] = _noncanonical_alias(parsed[field].encode()).decode()
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_INVALID"):
        protection._load_key(_json(parsed), kind)


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owner_only_key_files_read_exact_bytes(tmp_path, keys, mode):
    path = tmp_path / "synthetic.jwk"
    path.write_bytes(keys["encryption"])
    path.chmod(mode)
    assert protection.read_key_file(path) == keys["encryption"]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o601, 0o610])
def test_group_or_world_key_access_rejected(tmp_path, keys, mode):
    path = tmp_path / "synthetic.jwk"
    path.write_bytes(keys["verification"])
    path.chmod(mode)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_FILE_INVALID"):
        protection.read_key_file(path)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "directory", "missing", "empty", "oversize"])
def test_unsafe_key_file_kinds_rejected_without_blocking(tmp_path, keys, kind):
    path = tmp_path / "candidate"
    original = tmp_path / "original"
    original.write_bytes(keys["encryption"])
    original.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(original)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        os.link(original, path)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind in ("empty", "oversize"):
        path.write_bytes(b"" if kind == "empty" else b"x" * 4097)
        path.chmod(0o600)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_FILE_INVALID"):
        protection.read_key_file(path)


def test_key_file_wrong_owner_rejected(tmp_path, keys, monkeypatch):
    path = tmp_path / "synthetic.jwk"
    path.write_bytes(keys["encryption"])
    path.chmod(0o600)
    actual_uid = os.getuid()
    monkeypatch.setattr(protection.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_KEY_FILE_INVALID"):
        protection.read_key_file(path)


@pytest.mark.parametrize("kind", ["key", "archive"])
@pytest.mark.parametrize("field", ["st_size", "st_mtime_ns", "st_ctime_ns"])
def test_changed_file_metadata_during_read_rejected(bundle, tmp_path, keys, monkeypatch, kind, field):
    path = _archive(tmp_path, keys["encryption"] if kind == "key" else _wrap(bundle.read_bytes(), "encrypted", keys))
    path.chmod(0o600)
    fstat = protection.os.fstat
    calls = 0
    def changing_metadata(descriptor):
        nonlocal calls
        calls += 1
        actual = fstat(descriptor)
        values = {name: getattr(actual, name) for name in
                  ("st_mode", "st_uid", "st_size", "st_nlink", "st_mtime_ns", "st_ctime_ns")}
        if calls == 2:
            values[field] += 1
        return SimpleNamespace(**values)
    monkeypatch.setattr(protection.os, "fstat", changing_metadata)
    code = "BACKUP_PROTECTION_KEY_FILE_INVALID" if kind == "key" else "BACKUP_PROTECTION_ARCHIVE_CHANGED"
    with pytest.raises(BackupError, match=code):
        if kind == "key":
            protection.read_key_file(path)
        else:
            protection.verify_archive(path, **_read_options("encrypted", keys))
    assert calls == 2


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_protected_archive_file_kinds_rejected_without_blocking(bundle, tmp_path, keys, kind):
    path = tmp_path / "candidate"
    if kind == "symlink":
        path.symlink_to(bundle)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    with pytest.raises(BackupError):
        protection.verify_archive(path, mode="encrypted", encryption_key=keys["encryption"])


@pytest.mark.parametrize("limit", ["MAX_ENVELOPE_BYTES", "MAX_JWS_BYTES", "MAX_PAYLOAD_BYTES"])
def test_resource_limit_fails_before_restore(bundle, tmp_path, keys, monkeypatch, limit):
    mode = "signed" if limit == "MAX_JWS_BYTES" else "encrypted"
    path = _archive(tmp_path, _wrap(bundle.read_bytes(), mode, keys))
    monkeypatch.setattr(protection, limit, 8)
    _assert_rejected_before_restore(path, tmp_path, monkeypatch, **_read_options(mode, keys))


def test_header_limit_precedes_json_parser(bundle, tmp_path, keys, monkeypatch):
    parts = _wrap(bundle.read_bytes(), "signed", keys).split(b".")
    parts[0] = b"A" * 2049
    path = _archive(tmp_path, b".".join(parts))
    # Supply the validated key before installing the parser spy.
    authority = protection._load_key(keys["verification"], "verification")
    def forbidden(*args, **kwargs):
        pytest.fail("Oversize header reached JSON parser")
    monkeypatch.setattr(protection, "_json", forbidden)
    with pytest.raises(BackupError, match="BACKUP_PROTECTION_(HEADER_INVALID|ENCODING_INVALID)"):
        protection._decode(path.read_bytes(), "signed", None, authority)


@pytest.mark.parametrize("mode", MODES[1:])
def test_protected_creation_never_stages_plaintext_bundle(source, tmp_path, keys, monkeypatch, mode):
    serialize = backup._write_bundle
    link = backup.os.link
    seen = []
    def memory_only(stream, *args, **kwargs):
        assert isinstance(stream, io.BytesIO), "Protected ZIP serialization must remain in memory"
        return serialize(stream, *args, **kwargs)
    def inspect_publication(temporary, output, *args, **kwargs):
        raw = Path(temporary).read_bytes()
        assert not raw.startswith(b"PK")
        assert source[2] not in raw
        assert stat.S_IMODE(Path(temporary).stat().st_mode) == 0o600
        seen.append(raw)
        return link(temporary, output, *args, **kwargs)
    monkeypatch.setattr(backup, "_write_bundle", memory_only)
    monkeypatch.setattr(backup.os, "link", inspect_publication)
    path = tmp_path / "protected.pfb"
    protection.create_backup(source[0], source[1], path, **_write_options(mode, keys))
    assert seen == [path.read_bytes()]
    assert not list(tmp_path.glob(".pf-backup-*"))
    protection.verify_archive(path, **_read_options(mode, keys))


@pytest.mark.parametrize("failure", ["encoding", "publication-race", "size-limit"])
def test_failed_protected_creation_is_atomic(source, tmp_path, keys, monkeypatch, failure):
    output = tmp_path / "protected.pfb"
    if failure == "encoding":
        def fail(*args, **kwargs):
            raise BackupError("SYNTHETIC_ENCODING_FAILURE")
        monkeypatch.setattr(protection, "_encode", fail)
    elif failure == "publication-race":
        link = backup.os.link
        def race(temporary, target):
            Path(target).write_bytes(b"concurrent precious output")
            return link(temporary, target)
        monkeypatch.setattr(backup.os, "link", race)
    else:
        monkeypatch.setattr(protection, "MAX_PAYLOAD_BYTES", 8)
    with pytest.raises(BackupError):
        protection.create_backup(source[0], source[1], output, **_write_options("signed-encrypted", keys))
    if failure == "publication-race":
        assert output.read_bytes() == b"concurrent precious output"
    else:
        assert not output.exists()
    assert not list(tmp_path.glob(".pf-backup-*"))
    assert recovery_report(source[0])["state"] == "PAUSED"


@pytest.mark.parametrize("command", ["backup", "verify", "restore"])
def test_cli_requires_explicit_protection(tmp_path, capsys, command):
    arguments = {"backup": ["backup", "--database-url-file", str(tmp_path / "missing"),
                             "--documents", str(tmp_path), "--output", str(tmp_path / "out")],
                 "verify": ["verify", str(tmp_path / "missing")],
                 "restore": ["restore", str(tmp_path / "missing"), "--data-dir", str(tmp_path / "target")]}
    with pytest.raises(SystemExit) as caught:
        main(arguments[command])
    assert caught.value.code == 2
    assert "--protection" in capsys.readouterr().err
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("mode", MODES)
def test_cli_roundtrip_explicit_keys_receipts_and_no_ambient_credentials(source, tmp_path, keys, capsys, monkeypatch, mode):
    files = {}
    for kind in ("encryption", "signing", "verification"):
        path = tmp_path / f"ephemeral-{kind}.jwk"
        path.write_bytes(keys[kind])
        path.chmod(0o600)
        files[kind] = str(path)
    url = tmp_path / "synthetic-database-url"
    url.write_text(source[0].engine.url.render_as_string(hide_password=False))
    url.chmod(0o600)
    poison = tmp_path / "ambient-must-not-exist.sqlite3"
    monkeypatch.setenv("PF_DATABASE_URL", f"sqlite:///{poison}")
    monkeypatch.setenv("ERP_API_SECRET", "synthetic-never-print-this")
    path = tmp_path / "cli.pfb"
    write_args = ["--protection", mode]
    read_args = ["--protection", mode]
    if "encrypted" in mode:
        write_args += ["--encryption-key-file", files["encryption"]]
        read_args += ["--encryption-key-file", files["encryption"]]
    if "signed" in mode:
        write_args += ["--signing-key-file", files["signing"]]
        read_args += ["--verification-key-file", files["verification"]]
    assert main(["backup", "--database-url-file", str(url), "--documents", str(source[1]),
                 "--output", str(path), *write_args]) == 0
    assert json.loads(capsys.readouterr().out)["backup_protection"]["mode"] == mode
    assert main(["verify", str(path), *read_args]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["state"] == "verified"
    assert verified["backup_protection"]["provenance_verified"] is ("signed" in mode)
    target = tmp_path / "cli-restored"
    assert main(["restore", str(path), "--data-dir", str(target), *read_args]) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)["state"] == "RECOVERY"
    assert "synthetic-never-print-this" not in result.out + result.err
    assert keys["encryption"].decode() not in result.out + result.err
    assert keys["signing"].decode() not in result.out + result.err
    assert not poison.exists() and not (target / ".env").exists()


def test_cli_invalid_key_error_does_not_print_key_material(bundle, tmp_path, capsys):
    path = tmp_path / "invalid-ephemeral.jwk"
    path.write_bytes(b"synthetic-invalid-secret-that-must-never-be-printed")
    path.chmod(0o600)
    assert main(["verify", str(bundle), "--protection", "encrypted", "--encryption-key-file", str(path)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["error"] == "BACKUP_PROTECTION_KEY_INVALID"
    assert "synthetic-invalid-secret" not in output.out + output.err


@pytest.mark.parametrize("mode", MODES)
def test_cli_key_policy_precedes_database_url_or_connection(tmp_path, keys, capsys, monkeypatch, mode):
    import procureflow.backup_cli as cli
    key = tmp_path / "ephemeral-encryption.jwk"
    key.write_bytes(keys["encryption"])
    key.chmod(0o600)
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid key policy reached source URL or database")
    monkeypatch.setattr(cli, "Database", forbidden)
    monkeypatch.setattr(cli, "_database_url_file", forbidden)
    args = ["backup", "--protection", mode, "--database-url-file", str(tmp_path / "missing"),
            "--documents", str(tmp_path), "--output", str(tmp_path / "output")]
    if mode == "plain":
        args += ["--encryption-key-file", str(key)]
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "BACKUP_PROTECTION_KEYS_MISMATCH"
    assert not (tmp_path / "output").exists()
