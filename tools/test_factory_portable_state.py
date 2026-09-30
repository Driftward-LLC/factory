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
            if self.wrong_image:
                return "sha256:" + "0" * 64
            return args[-1] if args[-1].startswith("sha256:") else "sha256:" + hashlib.sha256(args[-1].encode()).hexdigest()
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

    def production(self) -> dict:
        marker_path = self.source / bootstrap.MARKER
        marker = json.loads(marker_path.read_text())
        marker["profile"] = state.PRODUCTION
        marker["image"] = "sha256:" + "c" * 64
        marker_path.write_text(json.dumps(marker))
        self.installation.values.update(FACTORY_IMAGE=marker["image"], FACTORY_POSTGRES_IMAGE="sha256:" + "a" * 64,
                                        FACTORY_HATCHET_IMAGE="sha256:" + "b" * 64,
                                        HATCHET_SERVER_URL="https://factory.example.test/hatchet")
        (self.source / ".env").write_text(bootstrap.env_text(self.installation.values))
        self.compose.return_value = json.dumps({"services": {
            key: {"image": self.installation.values[key]}
            for key in ("FACTORY_IMAGE", "FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE")
        }})
        provenance = {"source_project": "driftward-factory", "destination_project": "factory-source",
                      "factory_image": marker["image"],
                      "snapshot_sha256": "previous-snapshot", "activation_approved": True,
                      "profile_sha256": state._hash(ROOT / "deploy/compose.production.yml"),
                      "ledger_counts": {"ready_for_human_review": 6}}
        bootstrap._create_private(self.source / state.PROVENANCE, json.dumps(provenance))
        for suffix in state.VOLUMES[5:]:
            self.docker.volumes[f"factory-source-{suffix}"] = {
                "data": f"retained-{suffix}".encode(),
                "Labels": {"com.docker.compose.project": "factory-source",
                           "com.docker.compose.volume": "factory-" + suffix,
                           state.IDENTITY_LABEL: marker["installation_id"]},
            }
        return provenance

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

    def test_production_round_trip_preserves_state_and_requires_fresh_activation(self) -> None:
        original_provenance = self.production()
        self.backup()
        manifest = json.loads((self.snapshot / "manifest.json").read_text())
        self.assertEqual(manifest["profile"], state.PRODUCTION)
        self.assertEqual(manifest["volumes"], list(state.VOLUMES))
        self.assertEqual(manifest["deployment_sha256"], {
            name: state._hash(ROOT / "deploy" / name)
            for name in ("docker-compose.yml", "compose.portable.yml", "compose.production.yml")
        })
        self.assertEqual(manifest["images"], {
            self.installation.values[key]: self.installation.values[key]
            for key in ("FACTORY_IMAGE", "FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE")
        })
        self.assertEqual(manifest["sha256"]["config/" + state.PROVENANCE],
                         state._hash(self.source / state.PROVENANCE))
        state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True, port_offset=100)
        metadata, values = bootstrap.validate_directory(self.restored)
        self.assertEqual(metadata["profile"], state.PRODUCTION)
        self.assertNotEqual(metadata["installation_id"], manifest["installation_id"])
        for key in ("HATCHET_DEFAULT_TENANT_ID", "HATCHET_POSTGRES_PASSWORD", "HATCHET_ADMIN_PASSWORD", "HATCHET_SERVER_URL",
                    "HATCHET_SERVER_AUTH_COOKIE_SECRETS", "FACTORY_IMAGE", "FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE"):
            self.assertEqual(values[key], self.installation.values[key])
        self.assertEqual(values["FACTORY_CONSOLE_PORT"], str(bootstrap.PORTS["FACTORY_CONSOLE_PORT"] + 100))
        for name in state.CONFIG_FILES - {".env", bootstrap.MARKER}:
            self.assertEqual((self.source / name).read_bytes(), (self.restored / name).read_bytes())
        for suffix in state.VOLUMES:
            volume = self.docker.volumes[f"factory-restored-{suffix}"]
            self.assertEqual(volume["data"], self.docker.volumes[f"factory-source-{suffix}"]["data"])
            self.assertEqual(volume["Labels"][state.IDENTITY_LABEL], metadata["installation_id"])
        restored_provenance = json.loads((self.restored / state.PROVENANCE).read_text())
        self.assertEqual(restored_provenance, {
            **original_provenance, "destination_project": "factory-restored", "activation_approved": False,
            "predecessors": [{"project": "factory-source", "installation_id": manifest["installation_id"]}],
            "snapshot_sha256": state._hash(self.snapshot / "manifest.json"),
        })
        self.assertEqual(json.loads((self.source / state.PROVENANCE).read_text()), original_provenance)
        self.assertFalse(any("operator-socket" in name for name in self.docker.volumes))
        self.assertFalse(any(args[0] == "pull" for args in self.docker.calls))

    def test_production_restore_rejects_image_references_before_pull_or_writes(self) -> None:
        provenance = self.production()
        self.backup()
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        marker_path = self.snapshot / "config" / bootstrap.MARKER
        marker = json.loads(marker_path.read_text())
        env_path = self.snapshot / "config/.env"
        values = bootstrap.read_env(env_path)
        provenance_path = self.snapshot / "config" / state.PROVENANCE
        self.docker.calls.clear()
        for key in ("FACTORY_IMAGE", "FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE"):
            for reference in ("factory:test", "registry.example/factory@sha256:" + "d" * 64):
                changed = json.loads(json.dumps(manifest))
                changed["images"][reference] = changed["images"].pop(values[key])
                env_path.write_text(bootstrap.env_text({**values, key: reference}))
                marker_path.write_text(json.dumps({**marker, "image": reference if key == "FACTORY_IMAGE" else marker["image"]}))
                provenance_path.write_text(json.dumps({**provenance, "factory_image": reference if key == "FACTORY_IMAGE" else provenance["factory_image"]}))
                if key == "FACTORY_IMAGE":
                    changed["factory_image"] = reference
                for path in (env_path, marker_path, provenance_path):
                    changed["sha256"][path.relative_to(self.snapshot).as_posix()] = state._hash(path)
                manifest_path.write_text(json.dumps(changed))
                with self.subTest(key=key, reference=reference), self.assertRaises(ValueError):
                    state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
                self.assertFalse(self.restored.exists())
                self.assertTrue(all(args[0] == "ps" or args[:2] in (["network", "ls"], ["volume", "ls"])
                                    for args in self.docker.calls))

    def test_production_restore_rejects_extra_or_mismatching_image_manifest_before_writes(self) -> None:
        self.production()
        self.backup()
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.docker.calls.clear()
        for images in (
                {**manifest["images"], manifest["factory_image"]: "sha256:" + "e" * 64},
                {**manifest["images"], "registry.example/extra@sha256:" + "d" * 64: "sha256:" + "d" * 64}):
            manifest_path.write_text(json.dumps({**manifest, "images": images}))
            with self.subTest(images=images), self.assertRaises(ValueError):
                state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
            self.assertFalse(self.restored.exists())
            self.assertTrue(all(args[0] == "ps" or args[:2] in (["network", "ls"], ["volume", "ls"])
                                for args in self.docker.calls))

    def test_production_restore_requires_explicit_matching_wrapper(self) -> None:
        self.production()
        self.backup()
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, "matching operations wrapper"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.assertFalse(self.restored.exists())
        self.assertEqual(self.docker.calls, [])

    def test_production_wrapper_refuses_dry_run_snapshot(self) -> None:
        self.backup()
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, "matching operations wrapper"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
        self.assertFalse(self.restored.exists())
        self.assertEqual(self.docker.calls, [])

    def test_production_backup_requires_all_seven_volumes_before_stopping(self) -> None:
        self.production()
        for suffix in state.VOLUMES:
            name = "factory-source-" + suffix
            original = self.docker.volumes.pop(name)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "all required durable volumes"):
                self.backup()
            self.docker.volumes[name] = original
            self.assertFalse(self.snapshot.exists())
            self.assertNotIn(unittest.mock.call("stop"), self.compose.call_args_list)

    def test_production_restore_requires_all_seven_volumes_before_writes(self) -> None:
        self.production()
        self.backup()
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.docker.calls.clear()
        for suffix in state.VOLUMES:
            manifest_path.write_text(json.dumps({**manifest, "volumes": [item for item in state.VOLUMES if item != suffix]}))
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "invalid snapshot volume list"):
                state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
            self.assertFalse(self.restored.exists())
            self.assertEqual(self.docker.calls, [])

    def test_unknown_backup_profile_is_rejected_before_writes(self) -> None:
        marker_path = self.source / bootstrap.MARKER
        marker = json.loads(marker_path.read_text())
        marker["profile"] = "unknown-profile"
        marker_path.write_text(json.dumps(marker))
        with self.assertRaisesRegex(ValueError, "unsupported installation profile"):
            self.backup()
        self.assertFalse(self.snapshot.exists())
        self.compose.assert_not_called()
        self.assertEqual(self.docker.calls, [])

    def test_unknown_snapshot_profile_and_mismatching_marker_are_rejected_before_writes(self) -> None:
        self.production()
        self.backup()
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.docker.calls.clear()
        manifest_path.write_text(json.dumps({**manifest, "profile": "unknown-profile"}))
        with self.assertRaisesRegex(ValueError, "unsupported installation profile"):
            state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        marker_path = self.snapshot / "config" / bootstrap.MARKER
        marker = json.loads(marker_path.read_text())
        for profile in ("unknown-profile", "portable-dry-run", None):
            changed = {**marker, "profile": profile}
            if profile is None:
                changed.pop("profile")
            marker_path.write_text(json.dumps(changed))
            manifest["sha256"]["config/" + bootstrap.MARKER] = state._hash(marker_path)
            manifest_path.write_text(json.dumps(manifest))
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "profile differs"):
                state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
        self.assertFalse(self.restored.exists())
        self.assertEqual(self.docker.calls, [])

    def test_production_overlay_hash_tamper_is_rejected_before_writes(self) -> None:
        self.production()
        self.backup()
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.docker.calls.clear()
        for digest in ("foreign-overlay", None):
            if digest is None:
                manifest["deployment_sha256"].pop("compose.production.yml")
            else:
                manifest["deployment_sha256"]["compose.production.yml"] = digest
            manifest_path.write_text(json.dumps(manifest))
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, "same deployment bundle"):
                state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
        self.assertFalse(self.restored.exists())
        self.assertEqual(self.docker.calls, [])

    def test_production_backup_rejects_invalid_provenance_before_stopping(self) -> None:
        provenance = self.production()
        path = self.source / state.PROVENANCE
        cases = {
            "missing": (None, 0o600),
            "malformed": ("{", 0o600),
            "non-object": ("[]", 0o600),
            "invalid-approval": (json.dumps({**provenance, "activation_approved": "true"}), 0o600),
            "foreign-overlay": (json.dumps({**provenance, "profile_sha256": "foreign"}), 0o600),
            "non-private": (json.dumps(provenance), 0o644),
        }
        for name, (content, mode) in cases.items():
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
                path.chmod(mode)
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                self.backup()
            self.assertFalse(self.snapshot.exists())
            self.assertNotIn(unittest.mock.call("stop"), self.compose.call_args_list)
            self.assertFalse(any(args[0] in ("pull", "run") or args[:2] == ["volume", "create"]
                                 for args in self.docker.calls))

    def test_production_restore_rejects_invalid_provenance_before_writes(self) -> None:
        provenance = self.production()
        self.backup()
        path = self.snapshot / "config" / state.PROVENANCE
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.docker.calls.clear()
        cases = {
            "missing": (None, 0o600),
            "malformed": ("{", 0o600),
            "non-object": ("[]", 0o600),
            "invalid-approval": (json.dumps({**provenance, "activation_approved": "true"}), 0o600),
            "foreign-overlay": (json.dumps({**provenance, "profile_sha256": "foreign"}), 0o600),
            "non-private": (json.dumps(provenance), 0o644),
        }
        for name, (content, mode) in cases.items():
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
                path.chmod(mode)
                manifest["sha256"]["config/" + state.PROVENANCE] = state._hash(path)
                manifest_path.write_text(json.dumps(manifest))
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                state.restore(self.snapshot, self.restored, "factory-restored", apply=True, production=True)
            self.assertFalse(self.restored.exists())
            self.assertFalse(any(volume.startswith("factory-restored-") for volume in self.docker.volumes))
            self.assertFalse(any(args[0] in ("pull", "run") or args[:2] == ["volume", "create"]
                                 for args in self.docker.calls))


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
