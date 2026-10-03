from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "deploy/docker-compose.yml",
    "deploy/compose.portable.yml",
    "deploy/compose.executor.yml",
    "deploy/compose.production.yml",
    "deploy/factory-portable.py",
    "deploy/factory-cutover.py",
    "deploy/factory-production.py",
    "deploy/bootstrap.py",
    "deploy/portable_state.py",
    "deploy/PORTABLE.md",
    "deploy/EXECUTOR.md",
    "deploy/CUTOVER.md",
)
IMAGE = "ghcr.io/driftward-llc/factory@sha256:" + "a" * 64


class FactoryReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "source"
        (self.root / "deploy").mkdir(parents=True)
        shutil.copyfile(ROOT / "deploy/package-release.sh", self.root / "deploy/package-release.sh")
        for name in FILES:
            (self.root / name).write_text(f"public fixture for {name}\n", encoding="utf-8")
        (self.root / "deploy/private.env").write_text("DO_NOT_PACKAGE=private\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-qm", "fixture"], check=True,
        )
        self.commit = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        self.output = Path(self.directory.name) / "release"
        self.bundle = "factory-v2026.9.28-linux-amd64"

    def package(self, *extra: str, output: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "deploy/package-release.sh"), "--version", "v2026.9.28",
             "--image", IMAGE, "--output-dir", str(output or self.output), "--source-commit", self.commit, *extra],
            capture_output=True, text=True,
        )

    def test_bundle_is_allowlisted_pinned_and_reproducible(self) -> None:
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.output / f"{self.bundle}.tar.gz"
        with tarfile.open(archive) as bundle:
            self.assertEqual(
                {member.name for member in bundle.getmembers() if member.isfile()},
                {f"{self.bundle}/{name}" for name in (*FILES, "factory.env")},
            )
            metadata = bundle.extractfile(f"{self.bundle}/factory.env").read().decode()
            self.assertIn(f"FACTORY_IMAGE={IMAGE}\n", metadata)
            self.assertIn(f"FACTORY_RELEASE_COMMIT={self.commit}\n", metadata)
            self.assertIn("FACTORY_RELEASE_SCHEMA=1\n", metadata)
            self.assertIn("FACTORY_RELEASE_PLATFORM=linux/amd64\n", metadata)
            self.assertIn("FACTORY_RELEASE_VALIDATION_ONLY=false\n", metadata)
            self.assertTrue(all(member.uid == member.gid == 0 for member in bundle.getmembers()))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual((self.output / "SHA256SUMS").read_text(), f"{digest}  {archive.name}\n")
        second = self.output.with_name("second")
        self.assertEqual(self.package(output=second).returncode, 0)
        self.assertEqual(archive.read_bytes(), (second / archive.name).read_bytes())

    def test_unpinned_or_untrusted_metadata_is_rejected_before_output(self) -> None:
        for arguments in (
            ("--version", "../../outside"),
            ("--image", "ghcr.io/driftward-llc/factory:latest"),
            ("--image", "ghcr.io/another/factory@sha256:" + "a" * 64),
            ("--image", IMAGE + "\nSECRET=value"),
            ("--source-commit", "main"),
            ("--source-commit", "f" * 40),
        ):
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.package(*arguments).returncode, 0)
                self.assertFalse(self.output.exists())

    def test_missing_or_symlinked_inputs_are_rejected(self) -> None:
        source = self.root / FILES[0]
        source.unlink()
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())
        source.symlink_to(self.root / "deploy/private.env")
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_existing_artifacts_are_not_overwritten(self) -> None:
        self.assertEqual(self.package().returncode, 0)
        before = (self.output / "SHA256SUMS").read_bytes()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual((self.output / "SHA256SUMS").read_bytes(), before)

    def test_preview_is_distinct_and_marked_uninstallable(self) -> None:
        result = self.package("--validation-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("do not install", result.stdout)
        with tarfile.open(self.output / f"{self.bundle}-validation-only.tar.gz") as bundle:
            metadata = bundle.extractfile(f"{self.bundle}/factory.env").read().decode()
            self.assertIn("FACTORY_RELEASE_VALIDATION_ONLY=true\n", metadata)

    def test_executor_image_is_pinned_in_same_release_bundle(self) -> None:
        image = "ghcr.io/driftward-llc/factory-executor@sha256:" + "b" * 64
        result = self.package("--executor-image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.output / f"{self.bundle}.tar.gz") as bundle:
            metadata = bundle.extractfile(f"{self.bundle}/factory.env").read().decode()
            self.assertIn(f"FACTORY_EXECUTOR_IMAGE={image}\n", metadata)

    def test_bundled_clis_run_without_source_checkout(self) -> None:
        output = Path(self.directory.name) / "real-source-bundle"
        result = subprocess.run(
            ["bash", str(ROOT / "deploy/package-release.sh"), "--version", "v2026.9.28",
             "--image", IMAGE, "--output-dir", str(output), "--validation-only"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(
            ["tar", "-xzf", str(output / f"{self.bundle}-validation-only.tar.gz"), "-C", str(output)],
            check=True, capture_output=True,
        )
        for tool in ("factory-portable.py", "factory-cutover.py", "factory-production.py"):
            with self.subTest(tool=tool):
                help_result = subprocess.run(
                    ["python3", "-E", "-s", str(output / self.bundle / "deploy" / tool), "--help"],
                    cwd=output, capture_output=True, text=True,
                )
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("--directory", help_result.stdout)

    def test_release_writes_are_guarded_by_dry_run(self) -> None:
        workflow = (ROOT / ".github/workflows/factory-release.yml").read_text(encoding="utf-8")
        steps = workflow.split("      - name: ")
        for command in ("| docker login ghcr.io", "docker push", "git push origin", 'gh "${args[@]}"'):
            matching = [step for step in steps if command in step]
            self.assertEqual(len(matching), 1, command)
            self.assertIn("if: env.DRY_RUN != 'true'", matching[0])
        self.assertIn("docker build --platform linux/amd64", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("--validation-only", workflow)
        self.assertLess(workflow.index("python3 tools/check_factory_portable.py --image driftward/factory:ci"), workflow.index("docker push"))
        self.assertIn("refusing to overwrite: $image_tag", workflow)
        self.assertIn('"$RUNNER_TEMP/factory-release/SHA256SUMS"', workflow)
        self.assertNotIn("actions/upload-artifact", workflow)


if __name__ == "__main__":
    unittest.main()
