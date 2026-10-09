"""Explicit, bounded JOSE protection for the existing source-bound V1 backup.

Only compact JWS Ed25519 and compact JWE dir/A256GCM are accepted. Keys are
caller-managed minimal JWK bytes. No key generation, discovery, trust store,
network, environment, password KDF, automatic fallback or plaintext staging.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import stat

from jwcrypto import jwk, jwe, jws
from jwcrypto.common import JWException

from .backup import (BackupError, _backup_database, _restore_verified_backup,
                     _verify_backup_bytes, backup_database, verify_backup)

MODES = ("plain", "signed", "encrypted", "signed-encrypted")
# JOSE libraries hold multiple encoded/decoded copies. Deliberately below V1's
# 256 MiB ceiling; tokens cannot raise either ceiling. No compressed content.
MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_JWS_BYTES = ((MAX_PAYLOAD_BYTES + 2) // 3) * 4 + 2048
MAX_ENVELOPE_BYTES = ((MAX_JWS_BYTES + 2) // 3) * 4 + 2048
MAX_KEY_BYTES = 4096
ZIP_TYPE = "application/vnd.procureflow.paired-backup.v1+zip"
JWS_TYPE = "application/vnd.procureflow.paired-backup.v1+jws"
JWE_TYPE = "application/vnd.procureflow.paired-backup.v1+jwe"
_B64 = re.compile(rb"[A-Za-z0-9_-]*\Z")
_B64_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _fail(code):
    raise BackupError(code)


def _mode(mode):
    if not isinstance(mode, str) or mode not in MODES:
        _fail("BACKUP_PROTECTION_POLICY_REQUIRED")
    return "encrypted" in mode, "signed" in mode


def _json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("BACKUP_PROTECTION_INVALID_JSON")
            result[key] = value
        return result
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: _fail("BACKUP_PROTECTION_INVALID_JSON"))
    except (UnicodeError, ValueError, RecursionError):
        _fail("BACKUP_PROTECTION_INVALID_JSON")


def _unb64(value, *, size=None):
    if not isinstance(value, bytes) or not _B64.fullmatch(value) or len(value) % 4 == 1:
        _fail("BACKUP_PROTECTION_ENCODING_INVALID")
    try:
        raw = base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        _fail("BACKUP_PROTECTION_ENCODING_INVALID")
    if base64.urlsafe_b64encode(raw).rstrip(b"=") != value or (size is not None and len(raw) != size):
        _fail("BACKUP_PROTECTION_ENCODING_INVALID")
    return raw


def _load_key(content, kind):
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_KEY_BYTES:
        _fail("BACKUP_PROTECTION_KEY_INVALID")
    try:
        value = _json(content)
        keys = {"kty", "k"} if kind == "encryption" else {"kty", "crv", "x"}
        if kind == "signing":
            keys.add("d")
        if not isinstance(value, dict) or set(value) != keys or not all(isinstance(v, str) for v in value.values()):
            _fail("BACKUP_PROTECTION_KEY_INVALID")
        if kind == "encryption":
            if value["kty"] != "oct":
                _fail("BACKUP_PROTECTION_KEY_INVALID")
            _unb64(value["k"].encode("ascii"), size=32)
        else:
            if value["kty"] != "OKP" or value["crv"] != "Ed25519":
                _fail("BACKUP_PROTECTION_KEY_INVALID")
            _unb64(value["x"].encode("ascii"), size=32)
            if kind == "signing":
                _unb64(value["d"].encode("ascii"), size=32)
        key = jwk.JWK(**value)
        if kind == "signing":
            # Reject a private/public pair that does not match before taking the
            # source fence. The crypto library derives the public key.
            if key.get_op_key("sign").public_key().public_bytes_raw() != _unb64(value["x"].encode("ascii")):
                _fail("BACKUP_PROTECTION_KEY_INVALID")
        return key
    except (BackupError, JWException, ValueError, TypeError, UnicodeError):
        _fail("BACKUP_PROTECTION_KEY_INVALID")


def read_key_file(path: Path) -> bytes:
    """All explicit key files, including public pins, must be owner-only regular files.

    No following final symlinks, FIFO blocking, ambient key paths or key content
    in error messages. Operators must also protect ancestor directories.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) & 0o077 or not 1 <= before.st_size <= MAX_KEY_BYTES
                    or before.st_nlink != 1):
                _fail("BACKUP_PROTECTION_KEY_FILE_INVALID")
            content = stream.read(MAX_KEY_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                _fail("BACKUP_PROTECTION_KEY_FILE_INVALID")
            return content
    except OSError:
        _fail("BACKUP_PROTECTION_KEY_FILE_INVALID")


def _keys(mode, encryption_key, authority_key, *, writing):
    encrypted, signed = _mode(mode)
    if encrypted != (encryption_key is not None) or signed != (authority_key is not None):
        _fail("BACKUP_PROTECTION_KEYS_MISMATCH")
    encryption = _load_key(encryption_key, "encryption") if encrypted else None
    authority = _load_key(authority_key, "signing" if writing else "verification") if signed else None
    return encryption, authority


def _header(parts, expected):
    if not 1 <= len(parts[0]) <= 2048:
        _fail("BACKUP_PROTECTION_HEADER_INVALID")
    header = _json(_unb64(parts[0]))
    # No zip, jwk, jku, x5u, x5c, crit, b64, alternate algorithms or untrusted
    # key discovery. All accepted fields are integrity-protected by JOSE.
    if header != expected:
        _fail("BACKUP_PROTECTION_HEADER_INVALID")


def _signed_header(key):
    return {"alg": "Ed25519", "typ": JWS_TYPE, "cty": ZIP_TYPE, "kid": key.thumbprint()}


def _encrypted_header(signed):
    return {"alg": "dir", "enc": "A256GCM", "typ": JWE_TYPE, "cty": JWS_TYPE if signed else ZIP_TYPE}


def _parts(content, count, limit):
    if not isinstance(content, bytes) or not 1 <= len(content) <= limit:
        _fail("BACKUP_PROTECTION_LIMIT")
    if content.count(b".") != count - 1:
        _fail("BACKUP_PROTECTION_FORMAT_MISMATCH")
    parts = content.split(b".")
    # Check encoded alphabet before a library parser, not the large decoded
    # payload. Canonical base64 is separately checked for small crypto fields.
    for part in parts:
        remainder = len(part) % 4
        if (not _B64.fullmatch(part) or remainder == 1
                or (remainder == 2 and _B64_ALPHABET.index(part[-1]) & 15)
                or (remainder == 3 and _B64_ALPHABET.index(part[-1]) & 3)):
            _fail("BACKUP_PROTECTION_ENCODING_INVALID")
    return parts


def _decode(content, mode, encryption, authority):
    encrypted, signed = _mode(mode)
    try:
        if encrypted:
            parts = _parts(content, 5, MAX_ENVELOPE_BYTES)
            _header(parts, _encrypted_header(signed))
            if parts[1] != b"":  # dir has no encrypted CEK
                _fail("BACKUP_PROTECTION_FORMAT_MISMATCH")
            _unb64(parts[2], size=12)
            _unb64(parts[4], size=16)
            expected_limit = MAX_JWS_BYTES if signed else MAX_PAYLOAD_BYTES
            if not parts[3] or len(parts[3]) > ((expected_limit + 2) // 3) * 4:
                _fail("BACKUP_PROTECTION_LIMIT")
            token = jwe.JWE(algs=["dir", "A256GCM"])
            token.deserialize(content.decode("ascii"))
            token.decrypt(encryption)
            content = token.payload
            if not isinstance(content, bytes) or len(content) > expected_limit:
                _fail("BACKUP_PROTECTION_LIMIT")
        if signed:
            parts = _parts(content, 3, MAX_JWS_BYTES)
            _header(parts, _signed_header(authority))
            _unb64(parts[2], size=64)
            if not parts[1] or len(parts[1]) > ((MAX_PAYLOAD_BYTES + 2) // 3) * 4:
                _fail("BACKUP_PROTECTION_LIMIT")
            token = jws.JWS()
            token.allowed_algs = ["Ed25519"]
            token.deserialize(content.decode("ascii"))
            token.verify(authority, alg="Ed25519")
            content = token.payload
        if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_PAYLOAD_BYTES:
            _fail("BACKUP_PROTECTION_LIMIT")
        return content
    except BackupError:
        raise
    except (JWException, ValueError, TypeError, UnicodeError, KeyError):
        _fail("BACKUP_PROTECTION_AUTHENTICATION_FAILED")


def _encode(content, mode, encryption, authority):
    encrypted, signed = _mode(mode)
    raw = content
    if not 1 <= len(raw) <= MAX_PAYLOAD_BYTES:
        _fail("BACKUP_PROTECTION_LIMIT")
    try:
        if signed:
            token = jws.JWS(content)
            token.allowed_algs = ["Ed25519"]
            token.add_signature(authority, alg="Ed25519", protected=_signed_header(authority))
            content = token.serialize(compact=True).encode("ascii")
        if encrypted:
            token = jwe.JWE(content, protected=_encrypted_header(signed), algs=["dir", "A256GCM"])
            token.add_recipient(encryption)
            content = token.serialize(compact=True).encode("ascii")
        public = jwk.JWK.from_json(authority.export_public()) if signed else None
        if _decode(content, mode, encryption, public) != raw:
            _fail("BACKUP_PROTECTION_SELF_CHECK_FAILED")
        return content
    except BackupError:
        raise
    except (JWException, ValueError, TypeError):
        _fail("BACKUP_PROTECTION_ENCODING_FAILED")


def _receipt(mode, authority):
    return {"mode": mode, "provenance_verified": authority is not None,
            **({"signer_thumbprint": authority.thumbprint()} if authority is not None else {})}


def create_backup(db, document_dir: Path, output: Path, source_revision="unversioned", *,
                  mode: str, encryption_key: bytes | None = None, signing_key: bytes | None = None) -> dict:
    """Create only the requested profile; unused, missing or invalid keys reject."""
    encryption, signing = _keys(mode, encryption_key, signing_key, writing=True)
    if mode == "plain":
        result = backup_database(db, document_dir, output, source_revision)
    else:
        result = _backup_database(db, document_dir, output, source_revision,
                                 encode=lambda raw: _encode(raw, mode, encryption, signing),
                                 archive_limit=MAX_PAYLOAD_BYTES)
    return {**result, "backup_protection": _receipt(mode, signing)}


def verify_archive(path: Path, *, mode: str, encryption_key: bytes | None = None,
                   verification_key: bytes | None = None):
    """Verify exact caller policy, pinned provenance and the complete V1 payload."""
    encryption, authority = _keys(mode, encryption_key, verification_key, writing=False)
    if mode == "plain":
        return verify_backup(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_ENVELOPE_BYTES:
                _fail("BACKUP_PROTECTION_LIMIT")
            content = stream.read(MAX_ENVELOPE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                _fail("BACKUP_PROTECTION_ARCHIVE_CHANGED")
        raw = _decode(content, mode, encryption, authority)
        verified = _verify_backup_bytes(raw)
        return replace(verified, protection=_receipt(mode, authority))
    except OSError:
        _fail("BACKUP_PROTECTION_ARCHIVE_UNREADABLE")


def restore_archive(path: Path, data_dir: Path, *, mode: str, encryption_key: bytes | None = None,
                    verification_key: bytes | None = None, postgres_url: str | None = None):
    """Authenticate and validate fully before creating any restore resource."""
    verified = verify_archive(path, mode=mode, encryption_key=encryption_key, verification_key=verification_key)
    return _restore_verified_backup(verified, data_dir, postgres_url=postgres_url)
