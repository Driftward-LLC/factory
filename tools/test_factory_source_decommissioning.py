"""Reviewed retired-source recovery using synthetic state and read-only Docker mocks."""
from contextlib import contextmanager
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_factory_production as production_tests

production = production_tests.production


class SourceDecommissioningTests(unittest.TestCase):
    setUp = production_tests.ProductionTests.setUp

    def setup_inventory(self, *, present=True):
        mock = patch.object(production.os, "geteuid", return_value=0)
        mock.start()
        self.addCleanup(mock.stop)
        original_stat = Path.stat

        def root_provenance(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == self.directory / production.state.PROVENANCE:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        mock = patch.object(Path, "stat", root_provenance)
        mock.start()
        self.addCleanup(mock.stop)
        self.observed_ids, self.global_ids, self.mounts = [], [], {}
        source = self.provenance["source_project"]
        self.volumes = {source + "-" + suffix: {
            "Name": source + "-" + suffix, "Driver": "local", "Scope": "local",
            "CreatedAt": "2026-09-07T06:25:23Z", "Mountpoint": "/synthetic/" + suffix,
            "Options": None, "Labels": {"com.docker.compose.project": source,
                "com.docker.compose.volume": "factory-" + suffix}}
            for suffix in production.state.VOLUMES} if present else {}
        for name in ("assert_storage", "assert_candidate_writers", "assert_config"):
            mock = patch.object(self.install, name)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(production.state, "_docker", side_effect=self.docker)
        self.docker_mock = mock.start()
        self.addCleanup(mock.stop)

    def docker(self, args, env=None):
        if args == ["ps", "-aq", "--no-trunc"]:
            return "\n".join(self.global_ids)
        if args[:4] == ["ps", "-aq", "--no-trunc", "--filter"]:
            if args[4] == "label=com.docker.compose.project=" + self.provenance["source_project"]:
                return "\n".join(self.observed_ids)
            if args[4].startswith("volume="):
                return "\n".join(self.mounts.get(args[4][7:], []))
            if args[4] == "label=" + production.TENANT_LABEL + "=" + self.tenant:
                return ""
        if args == ["volume", "ls", "--format", "{{.Name}}"]:
            return "\n".join(self.volumes)
        if args[:2] == ["volume", "inspect"]:
            return json.dumps([self.volumes[name] for name in args[2:]])
        raise AssertionError("unexpected Docker operation: " + repr(args))

    def preview(self, operator="reviewed-operator", reason="Approved retired-source decommissioning"):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.install.record_source_decommissioning(operator, reason)
        return json.loads(output.getvalue())

    def apply(self, record):
        with patch("builtins.print"):
            self.install.record_source_decommissioning(record["operator"], record["reason"],
                approve_sha256=record["approval_sha256"], apply=True)
        self.install.provenance = production.Production(self.directory).provenance

    def test_unrecorded_empty_or_partial_inventory_still_refused(self):
        self.setup_inventory()
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            self.install.assert_source_fenced()
        for ids in ([self.provenance["source_container_ids"][0]], self.provenance["source_container_ids"], ["f" * 64]):
            self.observed_ids = ids
            with self.subTest(ids=len(ids)), self.assertRaisesRegex(ValueError, "every source container absent"):
                self.preview()

    def test_preview_apply_preserve_history_and_are_idempotent(self):
        self.setup_inventory()
        original = (self.directory / production.state.PROVENANCE).read_bytes()
        with patch.object(production.portable, "installation_lock") as lock:
            record = self.preview()
            lock.assert_not_called()
        self.assertEqual((self.directory / production.state.PROVENANCE).read_bytes(), original)
        self.assertTrue(all(record["source_volumes"].values()))
        self.apply(record)
        provenance = json.loads((self.directory / production.state.PROVENANCE).read_text())
        self.assertEqual({key: value for key, value in provenance.items() if key != "source_decommissioning"}, self.provenance)
        self.assertEqual((self.directory / production.state.PROVENANCE).stat().st_mode & 0o777, 0o600)
        self.install.assert_source_fenced()
        recorded = (self.directory / production.state.PROVENANCE).read_bytes()
        self.apply(record)
        self.assertEqual((self.directory / production.state.PROVENANCE).read_bytes(), recorded)
        for call in self.docker_mock.call_args_list:
            self.assertIn(call.args[0][0], ("ps", "volume"))
            if call.args[0][0] == "volume":
                self.assertIn(call.args[0][1], ("ls", "inspect"))

    def test_absent_volumes_recorded_and_recreation_or_removal_refused(self):
        self.setup_inventory(present=False)
        record = self.preview()
        self.assertEqual(set(record["source_volumes"].values()), {None})
        self.apply(record)
        self.install.assert_source_fenced()
        self.volumes[next(iter(record["source_volumes"]))] = {"Name": next(iter(record["source_volumes"]))}
        with self.assertRaises(ValueError):
            self.install.assert_source_fenced()

    def test_retained_volume_identity_changes_and_any_mount_refused(self):
        self.setup_inventory()
        record = self.preview()
        self.apply(record)
        name = next(iter(self.volumes))
        original = copy.deepcopy(self.volumes[name])
        for field, value in (("CreatedAt", "2026-10-13T00:00:00Z"), ("Labels", {}),
                             ("Driver", "another-driver"), ("Options", {"type": "foreign"}), ("Name", "foreign")):
            self.volumes[name] = {**original, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.install.assert_source_fenced()
        self.volumes[name] = original
        self.volumes.pop(name)
        with self.assertRaisesRegex(ValueError, "volumes changed"):
            self.install.assert_source_fenced()
        self.volumes[name] = original
        self.mounts[name] = ["foreign-stopped-or-running-container"]
        with self.assertRaisesRegex(ValueError, "still mounts"):
            self.install.assert_source_fenced()

    def test_original_ids_relabelled_and_source_resurrection_refused(self):
        self.setup_inventory()
        record = self.preview()
        self.apply(record)
        self.global_ids = [self.provenance["source_container_ids"][0]]
        with self.assertRaisesRegex(ValueError, "original source container"):
            self.install.assert_source_fenced()
        self.global_ids = []
        self.observed_ids = ["f" * 64]
        with self.assertRaisesRegex(ValueError, "every source container absent"):
            self.install.assert_source_fenced()

    def test_previously_reviewed_unlabelled_volumes_only(self):
        self.setup_inventory()
        name = next(iter(self.volumes))
        self.volumes[name]["Labels"] = None
        with self.assertRaisesRegex(ValueError, "ownership differs"):
            self.preview()
        self.install.provenance["unlabelled_source_volumes"] = [name]
        record = self.preview()
        self.assertIsNotNone(record["source_volumes"][name])

    def test_wrong_digest_root_and_changed_approval_do_not_write(self):
        self.setup_inventory()
        record = self.preview()
        original = (self.directory / production.state.PROVENANCE).read_bytes()
        for digest in (None, "f" * 64):
            with patch("builtins.print"), self.assertRaisesRegex(ValueError, "exact reviewed"):
                self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True, approve_sha256=digest)
        with patch.object(production.os, "geteuid", return_value=1000), self.assertRaisesRegex(ValueError, "requires root"):
            self.preview()
        with patch("builtins.print"), self.assertRaisesRegex(ValueError, "exact reviewed"):
            self.install.record_source_decommissioning("another-operator", record["reason"], apply=True,
                approve_sha256=record["approval_sha256"])
        self.assertEqual((self.directory / production.state.PROVENANCE).read_bytes(), original)

    def test_malformed_or_changed_record_identity_rejected_before_docker(self):
        self.setup_inventory()
        record = self.preview()
        mutations = [lambda r: r.update(format_version=True), lambda r: r.update(recorded_by_uid=False),
            lambda r: r.update(recorded_at="invalid"), lambda r: r.update(operator=""),
            lambda r: r.update(reason="control\ncharacter"), lambda r: r.update(source_volumes={}),
            lambda r: r.update(approval_sha256="f" * 64),
            lambda r: r["identity"].update(source_container_ids=["f" * 64]),
            lambda r: r["identity"].update(source_images={}),
            lambda r: r["identity"].update(installation_id="00000000-0000-4000-8000-000000000000"),
            lambda r: r["identity"].update(destination_project="factory-restored"),
            lambda r: r["identity"].update(tenant_id="00000000-0000-4000-8000-000000000000")]
        for index, mutate in enumerate(mutations):
            altered = copy.deepcopy(record)
            mutate(altered)
            (self.directory / production.state.PROVENANCE).write_text(json.dumps({**self.provenance, "source_decommissioning": altered}))
            with self.subTest(index=index), patch.object(production.state, "_docker") as docker:
                with self.assertRaises(ValueError):
                    production.Production(self.directory)
                docker.assert_not_called()

    def test_changed_provenance_or_last_moment_volume_drift_refused(self):
        self.setup_inventory()
        record = self.preview()
        original = (self.directory / production.state.PROVENANCE).read_bytes()
        with patch.object(production.state, "read_provenance", return_value={}), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "provenance changed"):
                self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True,
                    approve_sha256=record["approval_sha256"])
        volumes = record["source_volumes"]
        changed = {name: None for name in volumes}
        with patch.object(self.install, "absent_source_volumes", side_effect=[volumes, volumes, changed]), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "volumes changed"):
                self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True,
                    approve_sha256=record["approval_sha256"])
        self.assertEqual((self.directory / production.state.PROVENANCE).read_bytes(), original)

    def test_failed_atomic_replace_retains_original_provenance(self):
        self.setup_inventory()
        record = self.preview()
        original = (self.directory / production.state.PROVENANCE).read_bytes()
        with patch.object(production.os, "replace", side_effect=OSError("synthetic replace failure")), patch("builtins.print"):
            with self.assertRaises(OSError):
                self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True,
                    approve_sha256=record["approval_sha256"])
        self.assertEqual((self.directory / production.state.PROVENANCE).read_bytes(), original)
        pending = self.directory / (production.state.PROVENANCE + ".decommission-pending")
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        with patch("builtins.print"), self.assertRaises(FileExistsError):
            self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True,
                approve_sha256=record["approval_sha256"])

    def test_cli_apply_holds_installation_and_tenant_locks(self):
        self.setup_inventory()
        held = []

        @contextmanager
        def lock(directory):
            held.append(directory)
            try:
                yield
            finally:
                held.pop()

        def check(*args, **kwargs):
            self.assertEqual(held, [self.directory, Path("/tmp") / ("factory-tenant-" + self.tenant)])

        argv = ["production", "--directory", str(self.directory), "record-source-decommissioned",
                "--operator", "root-reviewer", "--reason", "approved", "--apply", "--approve-sha256", "f" * 64]
        with patch.object(sys, "argv", argv), patch.object(production.portable, "installation_lock", side_effect=lock), \
                patch.object(production, "Production", return_value=self.install), \
                patch.object(self.install, "record_source_decommissioning", side_effect=check):
            self.assertEqual(production.main(), 0)
        self.assertEqual(held, [])

    def test_cli_refuses_contended_installation_or_tenant_lock_before_recording(self):
        self.setup_inventory()
        argv = ["production", "--directory", str(self.directory), "record-source-decommissioned",
                "--operator", "root-reviewer", "--reason", "approved", "--apply", "--approve-sha256", "f" * 64]
        for directory in (self.directory, Path("/tmp") / ("factory-tenant-" + self.tenant)):
            with self.subTest(directory=directory), patch.object(production.os, "geteuid", return_value=os.getuid()), \
                    production.portable.installation_lock(directory), patch.object(sys, "argv", argv), \
                    patch.object(production, "Production", return_value=self.install), \
                    patch.object(self.install, "record_source_decommissioning") as record, \
                    patch("sys.stderr", new_callable=io.StringIO) as output:
                self.assertEqual(production.main(), 1)
                self.assertIn("private output withheld", output.getvalue())
                record.assert_not_called()

    def test_record_requires_root_owned_provenance_and_safe_review_text(self):
        self.setup_inventory()
        record = self.preview()
        original_stat = Path.stat

        def foreign_owner(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == self.directory / production.state.PROVENANCE:
                fields = list(info)
                fields[4] = 1000
                return os.stat_result(fields)
            return info

        with patch.object(Path, "stat", foreign_owner), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "root-owned"):
                self.install.record_source_decommissioning(record["operator"], record["reason"], apply=True,
                    approve_sha256=record["approval_sha256"])
            (self.directory / production.state.PROVENANCE).write_text(json.dumps({**self.provenance, "source_decommissioning": record}))
            with self.assertRaisesRegex(ValueError, "root-owned"):
                production.Production(self.directory)
        for operator, reason in (("", "reason"), ("operator", ""), ("operator", "bad\nreason"), ("operator", "x" * 1001)):
            with self.subTest(operator=operator, reason=reason[:12]), self.assertRaisesRegex(ValueError, "operator and review reason"):
                self.preview(operator, reason)


if __name__ == "__main__":
    unittest.main()
