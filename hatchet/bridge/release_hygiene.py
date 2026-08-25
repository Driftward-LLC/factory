#!/usr/bin/env python3
"""Fail-closed hygiene checks for immutable bridge releases.

The scanner intentionally reports only a relative path and a rule name.  It
never prints staged bytes or a protected value read from /etc/driftward.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
import test_capability


class ReleaseHygieneError(RuntimeError):
    pass


MIN_SECRET_BYTES = 12
MIN_SECRET_DISTINCT_BYTES = 4


@dataclass(frozen=True)
class ProtectedEnvFile:
    path: Path
    required: bool
    secret_keys: frozenset[str]
    optional_empty_keys: frozenset[str] = frozenset()
    owner_uid: int = 0
    owner_gid: int = 0
    mode: int = 0o600


# This exact production schema is the only one the public API and CLI may use.
# Files needed by the bridge, existing worker services, and their verifier are
# required. The optional entries correspond to units that deliberately use
# `EnvironmentFile=-` or are not installed until their scoped feature is
# enabled. A future secret-looking key in one of these files fails closed.
PRODUCTION_ENV_SCHEMA = (
    ProtectedEnvFile(Path("/etc/driftward/hatchet.env"), True, frozenset({
        "HATCHET_POSTGRES_PASSWORD", "HATCHET_SERVER_AUTH_COOKIE_SECRETS", "HATCHET_ADMIN_PASSWORD",
    })),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-bridge.env"), True, frozenset({
        "HATCHET_HERMES_BRIDGE_TOKEN", "HATCHET_HERMES_INSPECTION_TOKEN", "HATCHET_HERMES_RECOVERY_TOKEN",
    }), frozenset({"HATCHET_HERMES_INSPECTION_TOKEN", "HATCHET_HERMES_RECOVERY_TOKEN"})),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-workers.env"), True, frozenset({
        "HATCHET_CLIENT_TOKEN", "HATCHET_HERMES_BRIDGE_TOKEN",
    })),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-worker-bridge-executor.env"), True, frozenset({
        "HATCHET_HERMES_INSPECTION_TOKEN",
    }), frozenset({"HATCHET_HERMES_INSPECTION_TOKEN"})),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-verifier.env"), True, frozenset({"HATCHET_HERMES_VERIFIER_TOKEN"})),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-worker-reconciliation.env"), False, frozenset({
        "HATCHET_HERMES_RECOVERY_TOKEN",
    }), frozenset({"HATCHET_HERMES_RECOVERY_TOKEN"})),
    ProtectedEnvFile(Path("/etc/driftward/hatchet-event-ingress.env"), False, frozenset({
        "HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET",
    })),
    ProtectedEnvFile(Path("/etc/driftward/factory-operator.env"), False, frozenset({
        "HATCHET_FACTORY_OPERATOR_TOKEN", "HATCHET_CLIENT_TOKEN",
    })),
)
PRODUCTION_ENV_PATHS = tuple(policy.path for policy in PRODUCTION_ENV_SCHEMA)
SECRETISH_KEY = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|API_KEY|WEBHOOK)", re.I)
NONSECRET_SECRETISH_KEYS = frozenset()
REQUIRED_RELEASE_FILES = {
    "README.md",
    "predeploy-check.py",
    "worker-triggers/push-accepted-event.js",
}
GENERATED_BUNDLE_FILES = frozenset({"worker-triggers/push-accepted-event.js"})
METADATA_FILES = {"MANIFEST.json", "RELEASE_METADATA.json"}
DENIED_COMPONENTS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "dist", "build", ".npm", ".cache",
}
DENIED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".db", ".wal", ".shm", ".sock", ".log", ".env"}
HIGH_CONFIDENCE_RULES = (
    ("pem-private-key", re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("github-classic-token", re.compile(rb"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9]{36}(?![A-Za-z0-9_])")),
    ("github-fine-grained-token", re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])")),
    ("github-app-token", re.compile(rb"(?<![A-Za-z0-9_])gh[osru]_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])")),
    ("openai-token", re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9]{20,}(?![A-Za-z0-9_-])")),
    ("slack-token", re.compile(rb"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9_-])")),
    ("literal-authorization-token", re.compile(rb"authorization[ \t\r\n]*:[ \t\r\n]*(?:bearer|basic)[ \t\r\n]+[A-Za-z0-9._~+/=-]{12,}", re.I)),
)
TEXT_SPLIT_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".json"}
QUOTED_CONCAT_CHAIN = re.compile(
    rb"['\"](?:\\.|[^'\"\r\n])*['\"](?:[ \t\r\n]*\+[ \t\r\n]*['\"](?:\\.|[^'\"\r\n])*['\"])+"
)
QUOTED_LITERAL = re.compile(rb"['\"]([^'\"]*)['\"]")
MAX_QUOTED_CONCAT_CHAIN_BYTES = 8192


def _files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ReleaseHygieneError("release-tree:missing")
    paths: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseHygieneError(f"release-tree:symlink:{path.relative_to(root)}")
        if path.is_file():
            paths.append(path)
    return sorted(paths)


def _allowed_release_path(relative: Path) -> bool:
    text = relative.as_posix()
    if text in REQUIRED_RELEASE_FILES or text in METADATA_FILES:
        return True
    return relative.as_posix() == "test_capability.py" or (len(relative.parts) == 2 and relative.parts[0] == "hatchet_hermes_bridge" and relative.suffix == ".py")


def validate_release_tree(root: Path, *, require_metadata: bool = False) -> None:
    files = _files(root)
    seen = {path.relative_to(root).as_posix() for path in files}
    for path in files:
        relative = path.relative_to(root)
        if any(component in DENIED_COMPONENTS for component in relative.parts):
            raise ReleaseHygieneError(f"release-tree:denied-component:{relative}")
        if path.suffix.lower() in DENIED_SUFFIXES:
            raise ReleaseHygieneError(f"release-tree:denied-suffix:{relative}")
        if not _allowed_release_path(relative):
            raise ReleaseHygieneError(f"release-tree:unexpected-file:{relative}")
    missing = REQUIRED_RELEASE_FILES - seen
    if missing:
        raise ReleaseHygieneError(f"release-tree:missing-required:{sorted(missing)[0]}")
    package = [name for name in seen if name.startswith("hatchet_hermes_bridge/") and name.endswith(".py")]
    if "hatchet_hermes_bridge/__init__.py" not in package:
        raise ReleaseHygieneError("release-tree:missing-package-init")
    if require_metadata and not METADATA_FILES <= seen:
        raise ReleaseHygieneError("release-tree:missing-metadata")


def _secret_values_for_key(policy: ProtectedEnvFile, key: str, value: str) -> set[bytes]:
    if not value:
        if key in policy.optional_empty_keys:
            return set()
        raise ReleaseHygieneError(f"secret-env:empty-value:{policy.path.name}:{key}")
    values = value.split(",") if key == "HATCHET_SERVER_AUTH_COOKIE_SECRETS" else [value]
    encoded: set[bytes] = set()
    for item in values:
        candidate = item.encode("utf-8")
        if len(candidate) < MIN_SECRET_BYTES or len(set(candidate)) < MIN_SECRET_DISTINCT_BYTES:
            raise ReleaseHygieneError(f"secret-env:weak-value:{policy.path.name}:{key}")
        encoded.add(candidate)
    return encoded


def _validate_env_schema(schema: Sequence[ProtectedEnvFile], *, allow_test_paths: bool) -> None:
    paths = tuple(policy.path for policy in schema)
    if len(paths) != len(set(paths)):
        raise ReleaseHygieneError("secret-env:duplicate-path")
    if not allow_test_paths and paths != PRODUCTION_ENV_PATHS:
        raise ReleaseHygieneError("secret-env:unexpected-path")
    for policy in schema:
        if not policy.path.is_absolute():
            raise ReleaseHygieneError("secret-env:unexpected-path")


def _protected_values(schema: Sequence[ProtectedEnvFile], *, allow_test_paths: bool) -> set[bytes]:
    _validate_env_schema(schema, allow_test_paths=allow_test_paths)
    values: set[bytes] = set()
    for policy in schema:
        path = policy.path
        try:
            stat = path.lstat()
        except FileNotFoundError:
            if policy.required:
                raise ReleaseHygieneError(f"secret-env:required-file-missing:{path.name}") from None
            continue
        if path.is_symlink():
            raise ReleaseHygieneError(f"secret-env:unsafe-symlink:{path.name}")
        if not path.is_file():
            raise ReleaseHygieneError(f"secret-env:unsafe-type:{path.name}")
        if stat.st_nlink != 1:
            raise ReleaseHygieneError(f"secret-env:unsafe-hardlink:{path.name}")
        if stat.st_uid != policy.owner_uid or stat.st_gid != policy.owner_gid:
            raise ReleaseHygieneError(f"secret-env:unsafe-owner:{path.name}")
        if stat.st_mode & 0o777 != policy.mode:
            raise ReleaseHygieneError(f"secret-env:unsafe-mode:{path.name}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ReleaseHygieneError(f"secret-env:unreadable:{path.name}") from exc
        for line in lines:
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in policy.secret_keys:
                values.update(_secret_values_for_key(policy, key, value))
                continue
            if SECRETISH_KEY.search(key) and key not in NONSECRET_SECRETISH_KEYS:
                raise ReleaseHygieneError(f"secret-env:unclassified-key:{path.name}:{key}")
    return values


def _secret_variants(value: bytes) -> set[bytes]:
    standard = base64.b64encode(value)
    urlsafe = base64.urlsafe_b64encode(value)
    percent = urllib.parse.quote_from_bytes(value, safe="").encode("ascii")
    percent_lower_hex = re.sub(rb"%[0-9A-F]{2}", lambda match: match.group(0).lower(), percent)
    return {
        value,
        standard,
        standard.rstrip(b"="),
        urlsafe,
        urlsafe.rstrip(b"="),
        value.hex().encode("ascii"),
        value.hex().upper().encode("ascii"),
        percent,
        percent_lower_hex,
    }


def _quoted_concats(data: bytes) -> tuple[bytes, ...]:
    # This deliberately covers only direct quoted concatenation in authoritative
    # source/build text. It does not claim to recognize arbitrary transforms.
    return tuple(
        b"".join(QUOTED_LITERAL.findall(match.group(0)))
        for match in QUOTED_CONCAT_CHAIN.finditer(data)
        if len(match.group(0)) <= MAX_QUOTED_CONCAT_CHAIN_BYTES
    )


def _contains_quoted_concat(concats: Iterable[bytes], secret: bytes) -> bool:
    return (
        len(secret) >= MIN_SECRET_BYTES
        and all(0x20 <= byte <= 0x7E for byte in secret)
        and any(secret in concatenated for concatenated in concats)
    )


def _scan_quoted_concats(relative: Path) -> bool:
    text = relative.as_posix()
    return (
        text not in GENERATED_BUNDLE_FILES
        and not any(text.endswith(f"/{bundle}") for bundle in GENERATED_BUNDLE_FILES)
        and relative.suffix.lower() in TEXT_SPLIT_SUFFIXES
    )


def _scan_tree_with_schema(root: Path, schema: Sequence[ProtectedEnvFile], *, allow_test_paths: bool = True) -> None:
    """Private test seam; production callers use scan_tree's fixed schema."""
    protected_values = _protected_values(schema, allow_test_paths=allow_test_paths)
    for path in _files(root):
        relative = path.relative_to(root)
        data = path.read_bytes()
        concats = _quoted_concats(data) if _scan_quoted_concats(relative) else ()
        for secret in protected_values:
            if any(variant in data for variant in _secret_variants(secret)):
                raise ReleaseHygieneError(f"protected-secret-value:{relative}")
            if _contains_quoted_concat(concats, secret):
                raise ReleaseHygieneError(f"protected-secret-concat:{relative}")
        for rule, pattern in HIGH_CONFIDENCE_RULES:
            if pattern.search(data):
                raise ReleaseHygieneError(f"high-confidence-secret:{rule}:{relative}")


def scan_tree(root: Path) -> None:
    cap = test_capability.load()
    if cap is None:
        _scan_tree_with_schema(root, PRODUCTION_ENV_SCHEMA, allow_test_paths=False)
        return
    schema = tuple(ProtectedEnvFile(cap.root / policy.path.relative_to("/"), policy.required, policy.secret_keys, policy.optional_empty_keys, cap.uid, cap.gid, policy.mode) for policy in PRODUCTION_ENV_SCHEMA)
    _scan_tree_with_schema(root, schema, allow_test_paths=True)


def manifest_entries(root: Path) -> list[dict[str, str]]:
    validate_release_tree(root)
    entries = []
    for path in _files(root):
        if path.name in METADATA_FILES:
            continue
        entries.append({
            "path": str(path.relative_to(root)),
            "mode": format(path.stat().st_mode & 0o777, "04o"),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return entries


def write_manifest(root: Path, commit: str, source_tree: str) -> None:
    entries = manifest_entries(root)
    manifest = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    (root / "MANIFEST.json").write_text(manifest + "\n", encoding="utf-8")
    metadata = {
        "commit": commit,
        "source_tree": source_tree,
        "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "schema": "bridge-release-v1",
    }
    (root / "RELEASE_METADATA.json").write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def verify_manifest(root: Path, commit: str, source_tree: str) -> None:
    try:
        validate_release_tree(root, require_metadata=True)
        entries = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
        actual = manifest_entries(root)
        manifest = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        expected = {
            "commit": commit,
            "source_tree": source_tree,
            "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "schema": "bridge-release-v1",
        }
        if entries != actual or json.loads((root / "RELEASE_METADATA.json").read_text(encoding="utf-8")) != expected:
            raise ReleaseHygieneError("release-manifest:mismatch")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseHygieneError("release-manifest:invalid") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scan", "validate", "write-manifest", "verify"))
    parser.add_argument("--tree", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--source-tree")
    args = parser.parse_args()
    root = Path(args.tree)
    try:
        if args.command == "scan":
            scan_tree(root)
        elif args.command == "validate":
            validate_release_tree(root)
        elif args.command == "write-manifest":
            if not args.commit or not args.source_tree:
                raise ReleaseHygieneError("release-manifest:missing-provenance")
            write_manifest(root, args.commit, args.source_tree)
        else:
            if not args.commit or not args.source_tree:
                raise ReleaseHygieneError("release-manifest:missing-provenance")
            verify_manifest(root, args.commit, args.source_tree)
    except ReleaseHygieneError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
