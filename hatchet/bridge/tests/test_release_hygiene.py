import base64
from dataclasses import replace
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock
from pathlib import Path

import release_hygiene


HATCHET = Path(__file__).resolve().parents[2]
REPO = HATCHET.parent


def release_tree(root: Path) -> None:
    (root / "hatchet_hermes_bridge").mkdir(parents=True)
    (root / "worker-triggers").mkdir()
    (root / "README.md").write_text("runtime release\n")
    (root / "predeploy-check.py").write_text("print('ok')\n")
    (root / "hatchet_hermes_bridge" / "__init__.py").write_text("")
    (root / "hatchet_hermes_bridge" / "store.py").write_text("VALUE = 'safe'\n")
    (root / "worker-triggers" / "push-accepted-event.js").write_text("export {};\n")


class ReleaseHygieneTests(unittest.TestCase):
    def scan(self, root: Path, env_file: Path | None = None):
        if env_file is None:
            return release_hygiene._scan_tree_with_schema(root, ())
        policy = next(item for item in release_hygiene.PRODUCTION_ENV_SCHEMA if item.path.name == env_file.name)
        return release_hygiene._scan_tree_with_schema(
            root,
            (replace(policy, path=env_file, owner_uid=os.geteuid(), owner_gid=os.getegid()),),
        )

    def protected_env(self, directory: str, name: str) -> Path:
        path = Path(directory) / "protected" / name
        path.parent.mkdir()
        path.touch(mode=0o600)
        path.chmod(0o600)
        return path

    def fixture_schema(self, directory: str):
        root = Path(directory) / "protected"
        root.mkdir()
        return tuple(
            replace(policy, path=root / policy.path.name, owner_uid=os.geteuid(), owner_gid=os.getegid())
            for policy in release_hygiene.PRODUCTION_ENV_SCHEMA
        )

    def write_schema_fixture(self, schema):
        for policy in schema:
            if not policy.required:
                continue
            policy.path.touch(mode=0o600)
            policy.path.chmod(0o600)
            key = next(iter(policy.secret_keys))
            policy.path.write_text(f"{key}=fixture-secret-value-12345\n")

    def test_redaction_patterns_and_placeholders_are_not_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "redaction.py").write_text("PATTERN = r'(?:ghp_|github_pat_|Bearer\\\\s+)[A-Za-z0-9_.-]+'\n")
            (root / "placeholder.txt").write_text("ghp_<REDACTED> Authorization: Bearer <TOKEN>\n")
            self.assertIsNone(self.scan(root))

    def test_exact_protected_value_fails_without_disclosing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            secret = "private-fixture-value-12345"
            env_file = self.protected_env(directory, "hatchet-bridge.env")
            env_file.write_text(f"HATCHET_HERMES_BRIDGE_TOKEN={secret}\n")
            (root / "artifact.txt").write_text(secret)
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "protected-secret-value:artifact.txt") as caught:
                self.scan(root, env_file)
            self.assertNotIn(secret, str(caught.exception))

    def test_protected_value_comparison_preserves_bytes_after_equals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            secret = " secret-value-with-edge-space "
            env_file = self.protected_env(directory, "hatchet-bridge.env")
            env_file.write_text(f"HATCHET_HERMES_BRIDGE_TOKEN={secret}\n")
            (root / "artifact.txt").write_text(secret)
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "protected-secret-value:artifact.txt"):
                self.scan(root, env_file)

    def test_nonsecret_configuration_value_from_a_protected_env_is_not_compared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            value = "/etc/driftward/hatchet-verifier-policy.json"
            env_file = self.protected_env(directory, "hatchet-workers.env")
            env_file.write_text(f"HATCHET_VERIFIER_POLICY_PATH={value}\n")
            (root / "README.md").write_text(value)
            self.assertIsNone(self.scan(root, env_file))

    def test_secret_policy_rejects_short_required_values_and_allows_empty_optional_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = self.protected_env(directory, "hatchet-bridge.env")
            env_file.write_text("HATCHET_HERMES_BRIDGE_TOKEN=abc123\n")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^secret-env:weak-value:hatchet-bridge.env:HATCHET_HERMES_BRIDGE_TOKEN$"):
                self.scan(root, env_file)
            env_file.write_text("HATCHET_HERMES_INSPECTION_TOKEN=\n")
            self.assertIsNone(self.scan(root, env_file))

    def test_secret_policy_rejects_empty_required_and_unclassified_secretish_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = self.protected_env(directory, "hatchet-bridge.env")
            env_file.write_text("HATCHET_HERMES_BRIDGE_TOKEN=\n")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^secret-env:empty-value:hatchet-bridge.env:HATCHET_HERMES_BRIDGE_TOKEN$"):
                self.scan(root, env_file)
            env_file.write_text("HATCHET_NEW_TOKEN=adequately-long-value\n")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^secret-env:unclassified-key:hatchet-bridge.env:HATCHET_NEW_TOKEN$"):
                self.scan(root, env_file)

    def test_complete_canonical_fixture_and_optional_absence_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            schema = self.fixture_schema(directory)
            self.write_schema_fixture(schema)
            self.assertIsNone(release_hygiene._scan_tree_with_schema(root, schema))

    def test_each_required_protected_file_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            schema = self.fixture_schema(directory)
            self.write_schema_fixture(schema)
            for policy in schema:
                if not policy.required:
                    continue
                with self.subTest(file=policy.path.name):
                    policy.path.unlink()
                    with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:required-file-missing:{re.escape(policy.path.name)}$"):
                        release_hygiene._scan_tree_with_schema(root, schema)
                    key = next(iter(policy.secret_keys))
                    policy.path.write_text(f"{key}=fixture-secret-value-12345\n")
                    policy.path.chmod(0o600)

    def test_protected_file_safety_rejects_mode_owner_type_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            schema = self.fixture_schema(directory)
            self.write_schema_fixture(schema)
            policy = next(item for item in schema if item.required)
            policy.path.chmod(0o640)
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:unsafe-mode:{re.escape(policy.path.name)}$"):
                release_hygiene._scan_tree_with_schema(root, schema)
            policy.path.chmod(0o600)

            original_lstat = Path.lstat
            def wrong_owner(path):
                result = original_lstat(path)
                if path == policy.path:
                    fields = list(result); fields[4] += 1
                    return os.stat_result(fields)
                return result
            with mock.patch.object(release_hygiene.Path, "lstat", autospec=True, side_effect=wrong_owner):
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:unsafe-owner:{re.escape(policy.path.name)}$"):
                    release_hygiene._scan_tree_with_schema(root, schema)

            policy.path.unlink(); policy.path.mkdir()
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:unsafe-type:{re.escape(policy.path.name)}$"):
                release_hygiene._scan_tree_with_schema(root, schema)
            policy.path.rmdir(); target = Path(directory) / "target.env"; target.write_text("fixture")
            policy.path.symlink_to(target)
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:unsafe-symlink:{re.escape(policy.path.name)}$"):
                release_hygiene._scan_tree_with_schema(root, schema)
            policy.path.unlink(); os.link(target, policy.path)
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^secret-env:unsafe-hardlink:{re.escape(policy.path.name)}$"):
                release_hygiene._scan_tree_with_schema(root, schema)

    def test_schema_rejects_duplicate_and_unexpected_production_paths(self):
        duplicate = (release_hygiene.PRODUCTION_ENV_SCHEMA[0], release_hygiene.PRODUCTION_ENV_SCHEMA[0])
        with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^secret-env:duplicate-path$"):
            release_hygiene._validate_env_schema(duplicate, allow_test_paths=True)
        unexpected = (replace(release_hygiene.PRODUCTION_ENV_SCHEMA[0], path=Path("/tmp/unexpected.env")),)
        with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^secret-env:unexpected-path$"):
            release_hygiene._validate_env_schema(unexpected, allow_test_paths=False)

    def test_public_scanner_cannot_override_the_production_env_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(TypeError):
                release_hygiene.scan_tree(root, secret_env_files=())
            result = subprocess.run(
                [sys.executable, str(HATCHET / "bridge" / "release_hygiene.py"), "scan", "--tree", str(root), "--secret-env-file", "/tmp/attacker.env"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_pem_and_high_confidence_tokens_fail_in_text_or_binary(self):
        fixtures = (
            (b"-----BEGIN " + b"PRIVATE KEY-----\n", "pem-private-key"),
            (b"ghp_" + b"a" * 36, "github-classic-token"),
            (b"\x00github_pat_" + b"a" * 24, "github-fine-grained-token"),
        )
        for content, rule in fixtures:
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "generated.js").write_bytes(content)
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"high-confidence-secret:{rule}:generated.js"):
                    self.scan(root)

    def test_authorization_headers_detect_real_whitespace_case_and_binary_without_disclosure(self):
        token = "actual-header-token-12345"
        fixtures = (
            (b"Authorization: Bearer " + token.encode(), "normal"),
            (b"authorization\t:\tBASIC\t" + token.encode(), "case-spacing"),
            (b"\x00AUTHORIZATION \r\n :\r\n bearer\r\n " + token.encode() + b"\x00", "binary"),
        )
        for content, name in fixtures:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "generated.js").write_bytes(content)
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^high-confidence-secret:literal-authorization-token:generated.js$") as caught:
                    self.scan(root)
                self.assertNotIn(token, str(caught.exception))

    def test_authorization_header_near_misses_pass(self):
        fixtures = (
            b"Authorization:\\s*Bearer literal-regex-not-a-header",
            b"Authorization: Bearer short",
            b"Authorization: Digest sufficientlylongbutnotbearer",
            b"Authorization: Bearer <PLACEHOLDER-TOKEN>",
        )
        for content in fixtures:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "generated.js").write_bytes(content)
                self.assertIsNone(self.scan(root))

    def test_release_scan_is_independent_of_path(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            root = Path(directory)
            (root / "runtime.py").write_text("VALUE = 'ordinary'\n")
            self.assertIsNone(self.scan(root))

    def test_known_secret_raw_encodings_and_concat_are_detected_without_disclosure(self):
        secret = b"known/secret+value=12345"
        variants = {
            "raw": secret,
            "base64": base64.b64encode(secret),
            "base64-no-padding": base64.b64encode(secret).rstrip(b"="),
            "urlsafe-base64": base64.urlsafe_b64encode(secret),
            "urlsafe-base64-no-padding": base64.urlsafe_b64encode(secret).rstrip(b"="),
            "hex": secret.hex().encode(),
            "hex-uppercase": secret.hex().upper().encode(),
            "percent": urllib.parse.quote_from_bytes(secret, safe="").encode(),
            "percent-lower-hex": urllib.parse.quote_from_bytes(secret, safe="").replace("%2F", "%2f").replace("%2B", "%2b").replace("%3D", "%3d").encode(),
            "binary-nul": b"\x00" + secret + b"\x00",
        }
        for name, content in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "tree"; root.mkdir()
                env_file = self.protected_env(directory, "hatchet-bridge.env")
                env_file.write_bytes(b"HATCHET_HERMES_BRIDGE_TOKEN=" + secret + b"\n")
                (root / "generated.bin").write_bytes(content)
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "^protected-secret-value:generated.bin$") as caught:
                    self.scan(root, env_file)
                self.assertNotIn(secret.decode(), str(caught.exception))
        for suffix in (".js", ".ts", ".py"):
            with self.subTest(concat=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "tree"; root.mkdir()
                env_file = self.protected_env(directory, "hatchet-bridge.env")
                env_file.write_bytes(b"HATCHET_HERMES_BRIDGE_TOKEN=" + secret + b"\n")
                (root / f"generated{suffix}").write_text("'known/secret+' + \"value=12345\"")
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, f"^protected-secret-concat:generated{re.escape(suffix)}$"):
                    self.scan(root, env_file)

    def test_known_secret_near_misses_and_placeholders_pass(self):
        secret = b"known/secret+value=12345"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"; root.mkdir()
            env_file = self.protected_env(directory, "hatchet-bridge.env")
            env_file.write_bytes(b"HATCHET_HERMES_BRIDGE_TOKEN=" + secret + b"\n")
            (root / "generated.js").write_text("'known/secret+' + \"value=1234X\"\nTOKEN=<REDACTED>\n")
            self.assertIsNone(self.scan(root, env_file))

    def test_generated_trigger_bundle_skips_concat_scan_but_not_secret_scans(self):
        secret = b"known/secret+value=12345"
        for name, content, pattern in (
            ("direct", secret, "protected-secret-value"),
            ("high-confidence", b"Authorization: Bearer abcdefghijklmnop", "high-confidence-secret:literal-authorization-token"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "tree"
                (root / "worker-triggers").mkdir(parents=True)
                env_file = self.protected_env(directory, "hatchet-bridge.env")
                env_file.write_bytes(b"HATCHET_HERMES_BRIDGE_TOKEN=" + secret + b"\n")
                (root / "worker-triggers" / "push-accepted-event.js").write_bytes(content)
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, pattern):
                    self.scan(root, env_file)
        for parent in ("", "release"):
            with self.subTest(parent=parent or "root"), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "tree"
                trigger_dir = root / parent / "worker-triggers"
                trigger_dir.mkdir(parents=True)
                env_file = self.protected_env(directory, "hatchet-bridge.env")
                env_file.write_bytes(b"HATCHET_HERMES_BRIDGE_TOKEN=" + secret + b"\n")
                (trigger_dir / "push-accepted-event.js").write_text("'known/secret+' + \"value=12345\"\n")
                self.assertIsNone(self.scan(root, env_file))

    def test_release_allowlist_rejects_bytecode_caches_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_tree(root)
            (root / "hatchet_hermes_bridge" / "__pycache__").mkdir()
            (root / "hatchet_hermes_bridge" / "__pycache__" / "store.pyc").write_bytes(b"bytecode")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "denied-component"):
                release_hygiene.validate_release_tree(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_tree(root)
            (root / "unexpected.txt").write_text("not runtime")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "unexpected-file"):
                release_hygiene.validate_release_tree(root)

    def test_manifest_rejects_tampered_or_denied_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_tree(root)
            release_hygiene.write_manifest(root, "a" * 40, "b" * 40)
            release_hygiene.verify_manifest(root, "a" * 40, "b" * 40)
            (root / "worker-triggers" / "push-accepted-event.js").write_text("tampered")
            with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "release-manifest:mismatch"):
                release_hygiene.verify_manifest(root, "a" * 40, "b" * 40)

    def test_manifest_rejects_content_metadata_and_unexpected_file_drift(self):
        for drift in ("content", "metadata", "unexpected"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release_tree(root)
                release_hygiene.write_manifest(root, "a" * 40, "b" * 40)
                if drift == "content":
                    (root / "hatchet_hermes_bridge" / "store.py").write_text("VALUE = 'changed'\n")
                elif drift == "metadata":
                    (root / "hatchet_hermes_bridge" / "store.py").chmod(0o700)
                else:
                    (root / "unexpected.txt").write_text("drift\n")
                with self.assertRaisesRegex(release_hygiene.ReleaseHygieneError, "release-(?:tree|manifest):"):
                    release_hygiene.verify_manifest(root, "a" * 40, "b" * 40)

    def test_stage_only_release_from_a_clean_exact_commit_passes_prequiescence_gate(self):
        missing = [policy.path.name for policy in release_hygiene.PRODUCTION_ENV_SCHEMA if policy.required and not policy.path.exists()]
        if missing:
            self.skipTest("requires protected production env fixture unavailable in CI")
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "repo"
            shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "*.pyc", "dist"))
            subprocess.run(["git", "init", "-q"], cwd=copy, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=copy, check=True)
            subprocess.run(["git", "config", "user.name", "Release Hygiene Test"], cwd=copy, check=True)
            subprocess.run(["git", "add", "."], cwd=copy, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=copy, check=True)
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=copy, text=True).strip()
            stage = Path(directory) / "stage"
            result = subprocess.run(
                ["bash", str(copy / "hatchet" / "bridge" / "publish-release.sh"), "--stage-only"],
                cwd=copy,
                env={**os.environ, "HATCHET_BRIDGE_STAGE_ROOT": str(stage), "DRIFTWARD_BRIDGE_RELEASE_VERSION": commit},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            release = stage / "release"
            release_hygiene.verify_manifest(release, commit, subprocess.check_output(["git", "rev-parse", f"{commit}^{{tree}}"], cwd=copy, text=True).strip())
            self.assertFalse(any(path.suffix == ".pyc" or "__pycache__" in path.parts for path in release.rglob("*")))


if __name__ == "__main__":
    unittest.main()
