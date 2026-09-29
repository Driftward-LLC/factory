from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import portable_state as state
sys.path.pop(0)


class FakeDocker:
    def __init__(self, project: str, installation_id: str):
        self.calls = []
        self.running = False
        self.foreign_container = False
        self.wrong_image = False
        self.volumes = {f"{project}-{suffix}": {
            "data": f"retained-{suffix}".encode(),
            "Labels": {"com.docker.compose.project": project, "com.docker.compose.volume": "factory-" + suffix,
                       state.IDENTITY_LABEL: installation_id},
        } for suffix in state.VOLUMES[:5]}

    def __call__(self, args: list[str], env=None) -> str:
        self.calls.append(args)
        if args[:2] == ["volume", "ls"]:
            return "\n".join(self.volumes)
        if args[:2] == ["network", "ls"]:
            return ""
        if args[:2] == ["volume", "inspect"]:
            return json.dumps([self.volumes[args[-1]]], default=lambda value: value.decode())
        if args[:2] == ["volume", "create"]:
            labels = dict(args[index + 1].split("=", 1) for index, item in enumerate(args) if item == "--label")
            self.volumes.setdefault(args[-1], {"data": b"", "Labels": labels})
            return args[-1]
        if args[:2] == ["image", "inspect"]:
            return "sha256:" + ("0" * 64 if self.wrong_image else hashlib.sha256(args[-1].encode()).hexdigest())
        if args[0] == "pull":
            return ""
        if args[0] == "ps":
            return "writer" if (self.foreign_container if "--all" in args else self.running) else ""
        raise AssertionError(args)

    def tar(self, image, volume, directory, filename, *, restore, env=None):
        if restore:
            with tarfile.open(directory / filename) as archive:
                self.volumes[volume]["data"] = archive.extractfile("./ledger").read()
        else:
            with tarfile.open(directory / filename, "w") as archive:
                data = self.volumes[volume]["data"]
                info = tarfile.TarInfo("./ledger")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            (directory / filename).chmod(0o600)


class PortableStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.snapshot, self.restored = (self.root / name for name in ("source", "snapshot", "restored"))
        values = bootstrap.init_directory(self.source, "factory:test", "factory-source", apply=True)
        for name in state.CONFIG_FILES - {".env", bootstrap.MARKER}:
            path = self.source / name
            if not path.exists():
                bootstrap._create_private(path, "fixture-credential\n")
        metadata, _ = bootstrap.validate_directory(self.source)
        self.docker = FakeDocker("factory-source", metadata["installation_id"])
        self.compose = Mock(return_value=json.dumps({"services": {"factory": {"image": "factory:test"}, "postgres": {"image": "postgres@sha256:" + "a" * 64}}}))
        self.installation = SimpleNamespace(directory=self.source, project="factory-source", values=values, env={},
                                            compose=self.compose, assert_owned_resources=Mock())
        self.patcher = patch.object(state, "_docker", self.docker)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        tar_patcher = patch.object(state, "_tar", self.docker.tar)
        tar_patcher.start()
        self.addCleanup(tar_patcher.stop)

    def backup(self) -> None:
        state.backup(self.installation, self.snapshot, apply=True)

    def test_preview_does_not_stop_or_write(self) -> None:
        state.backup(self.installation, self.snapshot)
        self.assertFalse(self.snapshot.exists())
        self.assertNotIn(unittest.mock.call("stop"), self.compose.call_args_list)
        self.assertTrue(all(args[0] in ("image", "volume") for args in self.docker.calls))

    def test_cold_round_trip_preserves_credentials_keys_and_ledgers(self) -> None:
        self.backup()
        self.compose.assert_any_call("stop")
        self.installation.assert_owned_resources.assert_called_once()
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o700)
        state.restore(self.snapshot, self.restored, "factory-restored", apply=True, port_offset=100)
        metadata, values = bootstrap.validate_directory(self.restored)
        original, _ = bootstrap.validate_directory(self.source)
        self.assertNotEqual(metadata["installation_id"], original["installation_id"])
        self.assertEqual(values["HATCHET_POSTGRES_PASSWORD"], self.installation.values["HATCHET_POSTGRES_PASSWORD"])
        self.assertEqual(values["FACTORY_CONSOLE_PORT"], str(bootstrap.PORTS["FACTORY_CONSOLE_PORT"] + 100))
        self.assertEqual(values["FACTORY_INSTALL_DIR"], str(self.restored))
        for name in state.CONFIG_FILES - {".env", bootstrap.MARKER}:
            self.assertEqual((self.source / name).read_bytes(), (self.restored / name).read_bytes())
        for suffix in state.VOLUMES[:5]:
            volume = self.docker.volumes[f"factory-restored-{suffix}"]
            self.assertEqual(volume["data"], self.docker.volumes[f"factory-source-{suffix}"]["data"])
            self.assertEqual(volume["Labels"][state.IDENTITY_LABEL], metadata["installation_id"])
        self.assertFalse(any("operator-socket" in name for name in self.docker.volumes))
        self.assertTrue(any(args[0] == "pull" for args in self.docker.calls))

    def test_backup_refuses_writers_foreign_volumes_and_existing_destination(self) -> None:
        self.docker.running = True
        with self.assertRaisesRegex(ValueError, "running container"):
            self.backup()
        self.assertFalse(self.snapshot.exists())
        self.docker.running = False
        self.docker.volumes["factory-source-bridge-data"]["Labels"][state.IDENTITY_LABEL] = "foreign"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.backup()
        self.snapshot.mkdir()
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.backup()

    def test_restore_to_factory_preserves_other_projects_and_rejects_collisions(self) -> None:
        self.backup()
        original_volumes = dict(self.docker.volumes)
        for suffix in state.PROJECT_VOLUMES:
            name = f"factory-{suffix}"
            self.docker.volumes[name] = {}
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "already has volumes"):
                state.restore(self.snapshot, self.restored, "factory", apply=True)
            self.assertFalse(self.restored.exists())
            del self.docker.volumes[name]
        state.restore(self.snapshot, self.restored, "factory", apply=True)
        metadata, _ = bootstrap.validate_directory(self.restored)
        self.assertEqual(metadata["project"], "factory")
        for name, value in original_volumes.items():
            self.assertEqual(self.docker.volumes[name], value)
        for suffix in state.VOLUMES[:5]:
            self.assertEqual(self.docker.volumes[f"factory-{suffix}"]["data"],
                             original_volumes[f"factory-source-{suffix}"]["data"])

    def test_restore_preview_is_read_only_and_existing_targets_are_rejected(self) -> None:
        self.backup()
        self.docker.calls.clear()
        state.restore(self.snapshot, self.restored, "factory-restored")
        self.assertFalse(self.restored.exists())
        self.assertTrue(all(args[0] in ("ps", "volume", "network") for args in self.docker.calls))
        self.docker.foreign_container = True
        with self.assertRaisesRegex(ValueError, "already has containers"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.docker.foreign_container = False
        self.docker.volumes["factory-restored-bridge-data"] = {}
        with self.assertRaisesRegex(ValueError, "already has volumes"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.assertFalse(self.restored.exists())

    def test_checksum_symlink_and_cross_image_restore_are_rejected(self) -> None:
        self.backup()
        credential = self.snapshot / "config/env/hatchet-client.env"
        original = credential.read_bytes()
        credential.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        credential.write_bytes(original)
        credential.unlink()
        credential.symlink_to(self.source / "env/hatchet-client.env")
        with self.assertRaisesRegex(ValueError, "regular files"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        credential.unlink()
        bootstrap._create_private(credential, original.decode())
        self.docker.wrong_image = True
        with self.assertRaisesRegex(ValueError, "exact saved images"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.assertFalse(self.restored.exists())

    def test_archive_traversal_links_and_foreign_bundle_are_rejected(self) -> None:
        self.backup()
        archive_path = self.snapshot / "volumes/bridge-data.tar"
        for name, kind in (("../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                with tarfile.open(archive_path, "w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    archive.addfile(info)
                manifest_path = self.snapshot / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["sha256"]["volumes/bridge-data.tar"] = state._hash(archive_path)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "unsafe volume archive"):
                    state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        manifest["deployment_sha256"]["docker-compose.yml"] = "foreign"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "same deployment bundle"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.assertFalse(self.restored.exists())


class ArchiveStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.archive = self.directory / "volume.tar"
        self.payload = b"binary archive\x00\xff\x80"

    def test_backup_stream_is_private_and_owned_by_caller(self) -> None:
        def execute(args, **kwargs):
            self.assertEqual(args[:3], ["docker", "run", "--rm"])
            self.assertIn("type=volume,src=factory-test-data,dst=/state,readonly", args)
            self.assertNotIn("--user", args)  # Root must still read the volume's owned files.
            self.assertFalse(any("type=bind" in arg for arg in args))
            self.assertIn("-cf", args)
            self.assertEqual(args[args.index("-cf") + 1], "-")
            self.assertFalse(kwargs.get("text", False))
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            handle = kwargs["stdout"]
            info = os.fstat(handle.fileno())
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(info.st_mode & 0o777, 0o600)
            handle.write(self.payload)
            return subprocess.CompletedProcess(args, 0, stderr=b"")

        with patch.object(state.subprocess, "run", side_effect=execute):
            state._tar("factory:test", "factory-test-data", self.directory, self.archive.name, restore=False)
        self.assertEqual(self.archive.read_bytes(), self.payload)

    def test_restore_stream_uses_stdin_and_keeps_volume_writable(self) -> None:
        self.archive.write_bytes(self.payload)
        self.archive.chmod(0o600)

        def execute(args, **kwargs):
            self.assertIn("--interactive", args)
            self.assertIn("type=volume,src=factory-test-data,dst=/state", args)
            self.assertFalse(any("type=bind" in arg for arg in args))
            self.assertIn("-xf", args)
            self.assertEqual(args[args.index("-xf") + 1], "-")
            self.assertEqual(kwargs["stdin"].read(), self.payload)
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            self.assertFalse(kwargs.get("text", False))
            return subprocess.CompletedProcess(args, 0, stderr=b"")

        with patch.object(state.subprocess, "run", side_effect=execute):
            state._tar("factory:test", "factory-test-data", self.directory, self.archive.name, restore=True)
        self.assertEqual(self.archive.read_bytes(), self.payload)

    def test_backup_never_overwrites_existing_archive_or_symlink(self) -> None:
        self.archive.write_bytes(self.payload)
        alias = self.directory / "alias.tar"
        alias.symlink_to(self.archive)
        with patch.object(state.subprocess, "run") as execute:
            for path in (self.archive, alias):
                with self.subTest(path=path.name), self.assertRaises(OSError):
                    state._tar("factory:test", "factory-test-data", self.directory, path.name, restore=False)
            execute.assert_not_called()
        self.assertEqual(self.archive.read_bytes(), self.payload)

    def test_archive_failure_withholds_private_stderr(self) -> None:
        result = subprocess.CompletedProcess([], 1, stderr=b"private-key-and-token")
        with patch.object(state.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "withheld") as error:
                state._tar("factory:test", "factory-test-data", self.directory, self.archive.name, restore=False)
        self.assertNotIn("private-key-and-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
