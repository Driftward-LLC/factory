
from organization_fixtures import configure
setUpModule, tearDownModule = configure()
import unittest
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class InstallerPolicyTests(unittest.TestCase):

    def test_blocked_recovery_checks_ownership_and_accepts_only_absent_legacy_tables(self):
        source = (ROOT / "bridge" / "recover-blocked.sh").read_text()
        helper = source[source.index("ledger_snapshot() {"):source.index('expected_schema=')]
        guard = source[source.index('current_ledger="$(ledger_snapshot)"'):source.index("\nacquire_maintenance_lock")]
        ownership = ("factory_issue_controls", "factory_issue_control_commands")
        with tempfile.TemporaryDirectory() as directory:
            database, state = Path(directory) / "bridge.db", Path(directory) / "state"
            with sqlite3.connect(database) as conn:
                for table in ("bridge_jobs", "factory_attempts", "factory_execution_leases", "factory_attempt_outbox", "factory_inspection_outbox", "factory_attempt_events"):
                    conn.execute(f"CREATE TABLE {table} (id TEXT, evidence TEXT)")
            env = {**os.environ, "db": str(database), "state": str(state)}

            def run(command):
                return subprocess.run(["bash", "-c", helper + "\n" + command], env=env, text=True, capture_output=True)

            legacy = run("ledger_snapshot")
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            state.write_text("\n".join(line for line in legacy.stdout.splitlines() if not line.startswith(ownership)))
            self.assertEqual(run(guard).returncode, 0)
            for table in ownership:
                with self.subTest(table=table):
                    with sqlite3.connect(database) as conn:
                        conn.execute(f"CREATE TABLE {table} (id TEXT, evidence TEXT)")
                        conn.execute(f"INSERT INTO {table} VALUES ('existing','retained evidence')")
                    self.assertNotEqual(run(guard).returncode, 0)  # No captured authority for a new table.
                    state.write_text(run("ledger_snapshot").stdout)
                    self.assertEqual(run(guard).returncode, 0)
                    with sqlite3.connect(database) as conn:
                        conn.execute(f"UPDATE {table} SET evidence='unexpected rewrite'")
                    rejected = run(guard)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("recovery refuses changed immutable ledger", rejected.stderr)
                    with sqlite3.connect(database) as conn:
                        conn.execute(f"DROP TABLE {table}")
                    self.assertNotEqual(run(guard).returncode, 0)  # Missing no longer means legacy.
                    state.write_text(legacy.stdout)
            with sqlite3.connect(database) as conn:
                conn.execute("DROP TABLE bridge_jobs")
            unreadable = run(guard)
            self.assertNotEqual(unreadable.returncode, 0)
            self.assertIn("recovery could not read immutable ledger", unreadable.stderr)




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
        self.assertIn("GITLEAKS_VERSION", workflow)
        self.assertIn("/tmp/gitleaks git --redact --verbose .", workflow)

    def test_runtime_uses_one_explicit_policy_path_and_separate_verifier_env(self):
        compose = (ROOT.parent / "deploy" / "docker-compose.yml").read_text()
        smoke = (ROOT.parent / "deploy" / "factory-docker-smoke").read_text()
        operator = (ROOT / "bridge" / "hatchet_hermes_bridge" / "operator.py").read_text()
        verifier_env = (ROOT / "bridge" / "verifier-env.sh").read_text()
        executor_unit = (ROOT / "workers" / "systemd" / "factory-hatchet-worker-bridge-executor.service").read_text()
        self.assertIn('HATCHET_VERIFIER_POLICY_PATH: /etc/factory/config/verifier-policy.json', compose)
        self.assertIn('HATCHET_FACTORY_OPERATOR_RUNTIME: docker', compose)
        self.assertIn('expected_policy_path="/etc/factory/config/verifier-policy.json"', smoke)
        self.assertNotIn('HATCHET_VERIFIER_POLICY_PATH", "/etc/factory/hatchet-verifier-policy.json"', operator)
        self.assertNotIn('HATCHET_VERIFIER_POLICY_PATH:-/etc/factory/hatchet-verifier-policy.json', verifier_env)
        self.assertIn('EnvironmentFile=-/etc/factory/hatchet-verifier.env', executor_unit)

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
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
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
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/example/widgets/pull/20"}}),))
            conn.execute("INSERT INTO bridge_events VALUES ('b',?)", (json.dumps({"effect":{"pr_url":"https://github.com/example/widgets/pull/20"}}),))
            conn.execute("INSERT INTO bridge_events VALUES ('a','[]')")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors:", result.stdout)

    def test_predeploy_rejects_cross_repository_payload_and_event_refs(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            payload = json.dumps({"organization_binding": {"workspace_id": "11111111-1111-4111-8111-111111111111", "team_id": "22222222-2222-4222-8222-222222222222", "team_key": "ENG"}, "linear_issue":"ENG-8", "repo":"example/widgets", "dry_run":False})
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,NULL,'https://github.com/example/platform/pull/143','completed',NULL)", (payload,))
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/example/platform/pull/143"}}),))
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
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
            payload = json.dumps({"organization_binding": {"workspace_id": "11111111-1111-4111-8111-111111111111", "team_id": "22222222-2222-4222-8222-222222222222", "team_key": "ENG"}, "linear_issue":"ENG-8", "repo":"example/widgets", "dry_run":False})
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?,NULL,NULL,'completed',NULL)", (payload,))
            conn.execute("INSERT INTO factory_inspection_requests VALUES ('ENG-8','https://github.com/example/platform/pull/143',NULL)")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE factory_inspection_requests (url TEXT)")
            conn.execute("INSERT INTO factory_inspection_requests VALUES ('https://github.com/example/widgets/pull/20')")
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy import validation errors: 1", result.stdout)

    def test_predeploy_accepts_eng8_pr20_canonical_legacy_artifact(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        with tempfile.NamedTemporaryFile() as database:
            import sqlite3, json
            conn = sqlite3.connect(database.name)
            conn.execute("CREATE TABLE bridge_jobs (job_id TEXT, idempotency_key TEXT, payload_json TEXT, branch TEXT, pr_url TEXT, state TEXT, verification_original_context_json TEXT)")
            conn.execute("CREATE TABLE bridge_events (job_id TEXT, details_json TEXT)")
            payload = json.dumps({"organization_binding": {"workspace_id": "11111111-1111-4111-8111-111111111111", "team_id": "22222222-2222-4222-8222-222222222222", "team_key": "ENG"}, "linear_issue":"ENG-8", "repo":"example/widgets", "dry_run":False})
            branch = "codex/hatchet-hermes-d648b143d1aa45d1"
            conn.execute("INSERT INTO bridge_jobs VALUES ('a','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active',?, ?,NULL,'completed',NULL)", (payload, branch))
            conn.execute("INSERT INTO bridge_events VALUES ('a',?)", (json.dumps({"effect":{"pr_url":"https://github.com/example/widgets/pull/20", "head_branch":branch}}),))
            conn.commit(); conn.close()
            result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_predeploy_rejects_present_malformed_artifact_fields_in_every_source(self):
        check = ROOT / "bridge" / "predeploy-check.py"
        cases = (
            ("payload", {"pr_url": "https://example.invalid/pull/20"}),
            ("direct_url", "https://github.com/example/widgets/issues/20"),
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
                payload = {"organization_binding": {"workspace_id": "11111111-1111-4111-8111-111111111111", "team_id": "22222222-2222-4222-8222-222222222222", "team_key": "ENG"}, "linear_issue":"ENG-8", "repo":"example/widgets", "dry_run":False}
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
                result = subprocess.run(["python3", str(check), "--db", database.name, "--routing-registry", str(ROOT / "workers/test-fixtures/routing-registry.json"), "--validate-legacy-import"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
