import unittest
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class InstallerPolicyTests(unittest.TestCase):
    def test_predeploy_runs_before_any_install_or_secret_mutation(self):
        source = (ROOT / "bridge" / "install.sh").read_text()
        predeploy = source.index('predeploy-check.py')
        self.assertLess(predeploy, source.index('publish-release.sh" --stage-only'))
        self.assertLess(predeploy, source.index('BACKUP_PATH="$backup"'))
        self.assertLess(predeploy, source.rindex('systemctl daemon-reload'))
    def test_bridge_installer_stages_then_has_explicit_failure_boundaries_and_rollback(self):
        source = (ROOT / "bridge" / "install.sh").read_text()
        for stage in ("predeploy", "staging", "writer-quiesce", "backup", "disposable-migration", "live-migration", "config-publication", "policy-publication", "routing-publication", "unit-publication", "release-publication", "symlink-switch", "daemon-reload", "restart", "health"):
            self.assertIn(f"fail_at {stage}", source)
        self.assertIn('rollback()', source)
        self.assertIn('snapshot_targets()', source)
        self.assertIn('restore_targets()', source)
        self.assertIn('restore_database()', source)
        self.assertIn('bridge migration requires zero active jobs/attempts/leases/outboxes', source)
        self.assertIn('curl --silent --fail --max-time 2 http://127.0.0.1:19877/health', source)
        self.assertIn('install_maintenance_dropins', source)
        self.assertIn('stop_loaded_writers', source)
        self.assertIn('verify_maintenance_barrier', source)
        self.assertNotIn('mask --runtime', source)
        self.assertIn('writer-inventory.py', source)
        self.assertIn('publication-targets.sh', source)
        self.assertIn('safe_down_recovery.py" adopt', source)
        self.assertLess(source.index('safe_down_recovery.py" adopt'), source.index('fail_at predeploy'))
        self.assertIn('if (( recover_safe_down )); then\n    if (( live_migrated )); then restore_database', source)
        self.assertLess(source.index('hard_block_writers "safe-down'), source.index('if (( lock_released )); then'))
        barrier = (ROOT / "bridge" / "installer-barrier.sh").read_text()
        self.assertIn('flock -n 3', barrier)
        self.assertIn('ConditionPathExists=!', barrier)
        self.assertNotIn('HATCHET_BRIDGE_MAINTENANCE_BYPASS', source)
        self.assertNotIn('HATCHET_BRIDGE_RELEASE_ROOT:-', source)
        self.assertIn('driftward-hatchet-bridge-reconciler.service', source)
        self.assertIn('systemctl disable --now driftward-hatchet-bridge-reconciler.timer', source)
        self.assertIn('persist_recovery_state', source)
        self.assertIn('sha256sum "$backup"', source)
        self.assertIn('ledger-before.txt', source)
        self.assertIn('ledger-after.txt', source)
        self.assertLess(source.index('copy -- "$backup"') if 'copy -- "$backup"' in source else source.index('cp -- "$backup"'), source.index('PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$db" init'))
        self.assertLess(source.index('PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$db" init'), source.index('publication_commit.py" commit'))

    def test_staged_release_imports_are_bytecode_free_and_reproved_before_publication(self):
        source = (ROOT / "bridge" / "install.sh").read_text()
        publisher = (ROOT / "bridge" / "publish-release.sh").read_text()
        initial_validate = publisher.index('release_hygiene.py" validate --tree "$stage"')
        initial_scan = publisher.index('release_hygiene.py" scan --tree "$stage"')
        initial_manifest = publisher.index('release_hygiene.py" write-manifest --tree "$stage"')
        initial_verify = publisher.index('verify_release "$stage"')
        self.assertLess(initial_validate, initial_scan)
        self.assertLess(initial_scan, initial_manifest)
        self.assertLess(initial_manifest, initial_verify)
        self.assertIn('verify_staged_release()', source)
        self.assertEqual(source.count('verify_staged_release'), 4)
        disposable_init = source.index('PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$stage/disposable.db" init')
        disposable_predeploy = source.index('PYTHONPATH="$stage/release" python3 -B "$stage/release/predeploy-check.py"')
        live_init = source.index('PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$db" init')
        final_gate = source.index('live_migrated=1; snapshot_targets; verify_staged_release; publication_started=1')
        publication = source.index('publication_commit.py" commit')
        self.assertEqual(source.count('PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$stage/etc/driftward/hatchet-routing-registry.json" PYTHONPATH="$stage/release" python3 -B'), 4)
        self.assertIn('assert_migration_ledger "$stage/ledger-before.txt" "$stage/ledger-migrated.txt" "$expected_attestation_delta"', source)
        self.assertIn('--require-current-attestations', source)
        self.assertLess(disposable_init, disposable_predeploy)
        self.assertLess(disposable_predeploy, source.index('verify_staged_release', disposable_predeploy))
        self.assertLess(live_init, final_gate)
        self.assertLess(final_gate, publication)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$release_root/current" DB="$db" python3 -B', source)
        self.assertIn('PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$routing_registry" PYTHONPATH="$release_root/current" python3 -B', source)

    def test_rollback_tracks_absent_targets_symlinks_and_keeps_failed_writers_stopped(self):
        source = (ROOT / "bridge" / "install.sh").read_text()
        publication = (ROOT / "bridge" / "publication_commit.py").read_text()
        self.assertIn('rollback --journal', source)
        self.assertIn('publication-*.journal', publication)
        self.assertIn('rollback pre-state digest mismatch', publication)
        self.assertIn('restore_verified_backup', source)
        self.assertIn('stat -c \'%a\' "$backup"', source)
        self.assertIn('ROLLBACK BLOCKED: writers remain stopped', source)
        self.assertIn('failed_stages+=(restore-database)', source)
        self.assertIn('hard_block_writers "${failed_stages[*]}"', source)

    def test_blocked_recovery_is_proof_gated_and_never_migrates(self):
        source = (ROOT / "bridge" / "recover-blocked.sh").read_text()
        self.assertIn('recovery evidence missing', source)
        self.assertIn('recovery refuses changed immutable ledger', source)
        self.assertIn('install_maintenance_dropins', source)
        self.assertIn('verify_maintenance_barrier', source)
        self.assertIn('old bridge health failed', source)
        self.assertNotIn('python3 -m hatchet_hermes_bridge.cli', source)

    def test_safe_down_uses_fixed_held_capabilities_and_shared_target_inventory(self):
        recovery = (ROOT / "bridge" / "safe_down_recovery.py").read_text()
        wrapper = (ROOT / "bridge" / "recover-safe-down.sh").read_text()
        inventory = (ROOT / "bridge" / "publication-targets.sh").read_text()
        self.assertIn('O_NOFOLLOW', recovery)
        self.assertIn('os.link(transition, paths.transition', recovery)
        self.assertIn('_ensure_fd_matches(4, paths.transition', recovery)
        self.assertIn('_ensure_fd_matches(5, paths.marker', recovery)
        self.assertIn('_ensure_fd_matches(6, backup', recovery)
        self.assertIn('publication_targets', inventory)
        self.assertIn('safe_down_recovery.py" prepare --exec-install', wrapper)
        self.assertIn('safe_down_recovery.py" supersede', wrapper)
        self.assertIn('barrier_evidence_exec.py', wrapper)
        self.assertIn('--approved-pr-head', wrapper)
        self.assertIn('--parent-transition-sha256', wrapper)
        self.assertIn('adopt_inherited_maintenance_lock; install_maintenance_dropins', wrapper)
        self.assertIn('exec-existing', wrapper)
        self.assertIn('verify_existing_maintenance_barrier_readonly', wrapper)
        standalone = wrapper.split('if (( supersede )); then', 1)[1].split('fi\nadopt_inherited_maintenance_lock', 1)[0]
        for forbidden in ('install_maintenance_dropins', 'remove_maintenance_dropins', 'stop_loaded_writers', 'systemctl ', 'clear_maintenance_barrier'):
            self.assertNotIn(forbidden, standalone)
        self.assertIn('allow_publication_journal_replay', recovery)
        self.assertNotIn('def _validate_live_proof(record: dict[str, Any], paths: Paths, *,', recovery)
        self.assertIn('v1-publication-replay-needed', recovery)
        self.assertNotIn('installer-adoption-replay', recovery)
        self.assertIn('os.replace(temporary, paths.transition)', recovery)
        self.assertIn('github_evidence_sha256', recovery)

    def test_publish_release_is_commit_bound_and_manifest_verified(self):
        source = (ROOT / "bridge" / "publish-release.sh").read_text()
        self.assertIn('git -C "$repo_root" archive', source)
        self.assertNotIn('"$script_dir"/hatchet_hermes_bridge/*.py', source)
        self.assertIn('MANIFEST.json', source)
        self.assertIn('RELEASE_METADATA.json', source)
        self.assertIn('refusing production release from dirty relevant source', source)
        self.assertIn('existing release fails immutable manifest verification', source)
        self.assertIn('release_hygiene.py" scan --tree "$source_root"', source)
        self.assertIn('release_hygiene.py" scan --tree "$stage"', source)
        self.assertIn('release_hygiene.py" scan --tree "$target"', source)
        self.assertIn('release_hygiene.py" write-manifest', source)
        self.assertIn('rm -rf "$source_root/hatchet/bridge/tests"', source)
        self.assertIn("-name '*.test.ts' -delete", source)
        self.assertNotIn("grep -R -E '(ghp_", source)

    def test_release_hygiene_gate_is_self_contained_and_ci_owns_gitleaks(self):
        hygiene = (ROOT / "bridge" / "release_hygiene.py").read_text()
        workflow = (ROOT.parent / ".github" / "workflows" / "hatchet-quality.yml").read_text()
        self.assertNotIn("shutil.which", hygiene)
        self.assertNotIn("_scan_with_gitleaks", hygiene)
        self.assertNotIn("--skip-gitleaks", hygiene)
        self.assertNotIn("--secret-env-file", hygiene)
        self.assertIn('release_hygiene.py" scan --tree "$stage"', (ROOT / "bridge" / "install.sh").read_text())
        self.assertIn("GITLEAKS_VERSION", workflow)
        self.assertIn("/tmp/gitleaks git --redact --verbose .", workflow)

    def test_runtime_uses_one_explicit_policy_path_and_separate_verifier_env(self):
        compose = (ROOT.parent / "deploy" / "docker-compose.yml").read_text()
        smoke = (ROOT.parent / "deploy" / "factory-docker-smoke").read_text()
        operator = (ROOT / "bridge" / "hatchet_hermes_bridge" / "operator.py").read_text()
        verifier_env = (ROOT / "bridge" / "verifier-env.sh").read_text()
        executor_unit = (ROOT / "workers" / "systemd" / "driftward-hatchet-worker-bridge-executor.service").read_text()
        workers_install = (ROOT / "workers" / "install.sh").read_text()
        self.assertIn('HATCHET_VERIFIER_POLICY_PATH: /app/hatchet/workers/verifier-policy.json', compose)
        self.assertIn('HATCHET_FACTORY_OPERATOR_RUNTIME: docker', compose)
        self.assertIn('expected_policy_path="/app/hatchet/workers/verifier-policy.json"', smoke)
        self.assertNotIn('HATCHET_VERIFIER_POLICY_PATH", "/etc/driftward/hatchet-verifier-policy.json"', operator)
        self.assertNotIn('HATCHET_VERIFIER_POLICY_PATH:-/etc/driftward/hatchet-verifier-policy.json', verifier_env)
        self.assertIn('EnvironmentFile=-/etc/driftward/hatchet-verifier.env', executor_unit)
        self.assertIn('upsert_env "$common_env" HATCHET_VERIFIER_POLICY_PATH "$policy_dst"', workers_install)
        self.assertNotIn('HATCHET_VERIFIER_POLICY_PATH:-', workers_install)

    def test_verifier_env_requires_explicit_policy_path(self):
        helper = ROOT / "bridge" / "verifier-env.sh"
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "hatchet-verifier.env"
            env_file.write_text("HATCHET_HERMES_VERIFIER_TOKEN=" + "a" * 64 + "\n")
            env_file.chmod(0o600)
            result = subprocess.run(
                [str(helper), "status"],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **{key: value for key, value in os.environ.items() if key != "HATCHET_VERIFIER_POLICY_PATH"},
                    "HATCHET_VERIFIER_TEST_MODE": "1",
                    "HATCHET_VERIFIER_ENV_PATH": str(env_file),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("HATCHET_VERIFIER_POLICY_PATH is required", result.stderr)

    def test_verifier_env_provisioning_is_atomic_idempotent_and_non_disclosing(self):
        helper = ROOT / "bridge" / "verifier-env.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"; policy.write_text('{"version":1}')
            env_file = root / "hatchet-verifier.env"
            env = {**os.environ, "HATCHET_VERIFIER_TEST_MODE": "1", "HATCHET_VERIFIER_ENV_PATH": str(env_file), "HATCHET_VERIFIER_POLICY_PATH": str(policy), "HATCHET_VERIFIER_POLICY_SOURCE": str(policy)}
            first = subprocess.run([str(helper), "provision"], check=True, capture_output=True, text=True, env=env)
            before = env_file.read_text()
            second = subprocess.run([str(helper), "provision"], check=True, capture_output=True, text=True, env=env)
            status = subprocess.run([str(helper), "status"], check=True, capture_output=True, text=True, env=env)
            self.assertEqual(first.stdout + first.stderr + second.stdout + second.stderr, "")
            self.assertEqual(before, env_file.read_text())
            self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
            self.assertEqual(status.stdout.strip(), "verifier preflight ok")

    def test_predeploy_check_reports_legacy_ready_rows_without_exposing_evidence(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (state TEXT, verification_original_context_json TEXT)")
            conn.execute("INSERT INTO bridge_jobs VALUES ('ready_for_human_review', NULL)"); conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--require-zero-legacy-ready"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.strip(), "legacy ready receipts: 1")

    def test_predeploy_legacy_validation_blocks_malformed_eng8_style_row_before_init(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("INSERT INTO bridge_jobs VALUES ('legacy', 'hatchet:work-item:linear:eng-8:create_draft_pr:real:active', '{', 'branch', NULL, 'completed', NULL)")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors: 1", result.stdout)

    def test_predeploy_blocks_nonobject_event_only_and_duplicate_artifact_refs(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            first = json.dumps({"linear_issue":"ENG-8", "dry_run":False})
            second = json.dumps({"linear_issue":"ENG-9", "dry_run":False})
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,'codex/hatchet-hermes-same',NULL,'completed',NULL)", (first,))
            conn.execute("INSERT INTO bridge_jobs VALUES ('b','hatchet:work-item:linear:ENG-9:create_draft_pr:real:active',?,'codex/hatchet-hermes-same',NULL,'completed',NULL)", (second,))
            # ENG-8's historical PR reference appears only in an event.
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/Driftward-LLC/capykit/pull/20"}}),))
            conn.execute("INSERT INTO bridge_events VALUES ('b',?)", (json.dumps({"effect":{"pr_url":"https://github.com/Driftward-LLC/capykit/pull/20"}}),))
            conn.execute("INSERT INTO bridge_events VALUES ('a','[]')")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors:", result.stdout)

    def test_predeploy_rejects_cross_repository_payload_and_event_refs(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            payload = json.dumps({"linear_issue":"ENG-8", "repo":"Driftward-LLC/capykit", "dry_run":False})
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,NULL,'https://github.com/Driftward-LLC/driftward-host/pull/143','completed',NULL)", (payload,))
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/Driftward-LLC/driftward-host/pull/143"}}),))
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors: 1", result.stdout)

    def test_predeploy_rejects_inspection_only_mismatch_and_populated_old_schema(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            conn.execute("CREATE TABLE factory_inspection_requests (work_item_key TEXT, url TEXT, result_json TEXT)")
            payload = json.dumps({"linear_issue":"ENG-8", "repo":"Driftward-LLC/capykit", "dry_run":False})
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,NULL,NULL,'completed',NULL)", (payload,))
            conn.execute("INSERT INTO factory_inspection_requests VALUES ('ENG-8','https://github.com/Driftward-LLC/driftward-host/pull/143',NULL)")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE factory_inspection_requests (url TEXT)")
            conn.execute("INSERT INTO factory_inspection_requests VALUES ('https://github.com/Driftward-LLC/capykit/pull/20')")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors: 1", result.stdout)

    def test_predeploy_accepts_eng8_pr20_canonical_legacy_artifact(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            payload = json.dumps({"linear_issue":"ENG-8", "repo":"Driftward-LLC/capykit", "dry_run":False})
            branch = "codex/hatchet-hermes-d648b143d1aa45d1"
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?, ?,NULL,'completed',NULL)", (payload, branch))
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/Driftward-LLC/capykit/pull/20", "head_branch":branch}}),))
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_predeploy_rejects_present_malformed_artifact_fields_in_every_source(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        cases = (
            ("payload", {"pr_url": "https://example.invalid/pull/20"}),
            ("direct_url", "https://github.com/Driftward-LLC/capykit/issues/20"),
            ("direct_branch", "codex/not-a-factory-branch"),
            ("event", {"result": {"head_branch": ["codex/hatchet-hermes-0123456789abcdef"]}}),
            ("inspection", {"url": {"not": "a string"}}),
        )
        for source, malformed in cases:
            with self.subTest(source=source), tempfile.NamedTemporaryFile() as database:
                import sqlite3, json
                conn = sqlite3.connect(database.name)
                conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
                conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
                payload = {"linear_issue":"ENG-8", "repo":"Driftward-LLC/capykit", "dry_run":False}
                branch = None; pr_url = None
                if source == "payload": payload.update(malformed)
                if source == "direct_url": pr_url = malformed
                if source == "direct_branch": branch = malformed
                conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,?,?,?,NULL)", (json.dumps(payload), branch, pr_url, "completed"))
                if source == "event":
                    conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps(malformed),))
                else:
                    conn.execute("INSERT INTO bridge_events VALUES ('a','{}')")
                if source == "inspection":
                    conn.execute("CREATE TABLE factory_inspection_requests (work_item_key TEXT, url TEXT, result_json TEXT)")
                    conn.execute("INSERT INTO factory_inspection_requests VALUES ('ENG-8',?,NULL)", (json.dumps(malformed["url"]),))
                conn.commit(); conn.close()
                result = subprocess.run(["python3", str(check), "--db", database.name, "--validate-legacy-import"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
