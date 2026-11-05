import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / 'deploy/independent-reviewer'
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location('local_source_review', DIRECTORY / 'local_source_review.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalSourceReviewTests(unittest.TestCase):
    def test_source_tools_never_execute_repository_commands(self):
        for tool in ['run_check', 'start_preview', 'browser_check', 'terminal']:
            with self.subTest(tool=tool), patch.object(MODULE, 'command') as command:
                with self.assertRaises(ValueError):
                    MODULE.source_tool(tool, {}, ['git'], 'a'*40, 'b'*40)
                command.assert_not_called()

    def test_file_read_is_immutable_and_bounded(self):
        with patch.object(MODULE, 'command', side_effect=['4', 'text']) as command:
            response = MODULE.source_tool('read_file', {'path': 'src/app.py'}, ['git'], 'a'*40, 'b'*40)
        self.assertEqual(json.loads(response[0]['text']), 'text')
        self.assertEqual(command.call_args_list[-1].args[0], ['git', 'show', 'a'*40 + ':src/app.py'])
        with patch.object(MODULE, 'command', return_value='64001') as command:
            with self.assertRaises(ValueError):
                MODULE.source_tool('read_file', {'path': 'big'}, ['git'], 'a'*40, 'b'*40)
            self.assertEqual(command.call_count, 1)
        for path in ['/opt/data/auth.json', '../auth.json', 'src/../../auth', 'src//app', 'src\\app']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                MODULE.source_tool('read_file', {'path': path}, ['git'], 'a'*40, 'b'*40)

    def test_findings_require_existing_immutable_lines(self):
        finding = {'priority': 1, 'file': 'app.py', 'line': 2, 'title': 'Fix this', 'body': 'Evidence'}
        with patch.object(MODULE, 'command', side_effect=['4', 'text']):
            with self.assertRaisesRegex(ValueError, 'line does not exist'):
                MODULE.source_tool('validate_findings', {'findings': [finding]}, ['git'], 'a'*40, 'b'*40)

    def test_prompt_cannot_introduce_arbitrary_source_tool_arguments(self):
        with self.assertRaises(ValueError):
            MODULE.source_tool('diff', {'argv': ['sh']}, ['git'], 'a'*40, 'b'*40)

    def fixture_request(self):
        return {'intent': {'head_sha': 'a'*40, 'repository': 'example/demo', 'expected_base': 'main',
                           'pr_url': 'https://github.com/example/demo/pull/7', 'review_id': 'review-7', 'pass_number': 1,
                           'requirements': {'title': 'Example', 'description': 'Bounded acceptance'}},
                'session': 'factory-reviewer:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'instructions': 'Review'}

    def test_head_drift_refuses_before_native_launch(self):
        def command(args, **kwargs):
            return 'b'*40 if 'rev-parse' in args else ''
        with patch.object(MODULE, 'command', side_effect=command), patch.object(MODULE.subprocess, 'Popen') as launch:
            value = MODULE.run(self.fixture_request())
        self.assertEqual(value['reason'], 'review_head_changed')
        launch.assert_not_called()

    def test_native_source_protocol_stops_exact_session_and_rechecks_head(self):
        import io
        from unittest.mock import MagicMock
        process = MagicMock(stdin=io.BytesIO())
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if 'rev-parse' in args:
                return ('a' if 'head' in args[-1] else 'b')*40
            if 'diff' in args:
                return 'bounded diff'
            return ''
        frames = [json.dumps({'tool': 'diff', 'arguments': {}}).encode(),
                  json.dumps({'assessment': {'verdict': 'passed', 'findings': []}}).encode()]
        with patch.object(MODULE, 'command', side_effect=command), patch.object(MODULE.subprocess, 'Popen', return_value=process), patch.object(MODULE, 'relay_line', side_effect=frames):
            value = MODULE.run(self.fixture_request())
        self.assertEqual(value['verdict'], 'passed')
        frame = json.loads(process.stdin.getvalue().splitlines()[0])
        self.assertEqual(frame['telemetry'], {'role': 'reviewer', 'review_id': 'review-7',
            'repository': 'example/demo', 'head_sha': 'a'*40, 'pass_number': 1})
        from review_observer import ReviewObserver
        observer = ReviewObserver(frame['telemetry'], self.fixture_request()['session'], set(),
            {'POSTHOG_API_KEY': 'phc_synthetic', 'POSTHOG_HOST': 'https://example.invalid'})
        self.assertTrue(observer.enabled)
        stop = [args for args in calls if '--stop' in args]
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0][-1], self.fixture_request()['session'])
        process.kill.assert_called_once()
        self.assertEqual(sum('fetch' in args for args in calls), 2)

    def test_failed_exact_stop_never_returns_a_pass(self):
        from unittest.mock import MagicMock
        def command(args, **kwargs):
            if '--stop' in args:
                raise ValueError('Ambiguous stop')
            if 'rev-parse' in args:
                return ('a' if 'head' in args[-1] else 'b')*40
            return ''
        with patch.object(MODULE, 'command', side_effect=command), patch.object(MODULE.subprocess, 'Popen', return_value=MagicMock()), patch.object(MODULE, 'relay_line', return_value=json.dumps({'assessment': {'verdict': 'passed', 'findings': []}}).encode()):
            with self.assertRaisesRegex(ValueError, 'Ambiguous stop'):
                MODULE.run(self.fixture_request())

    def test_source_output_keeps_leading_blank_lines_and_true_finding_line(self):
        text = "\n\nraise RuntimeError('example')\n"
        self.assertEqual(MODULE.command([sys.executable, '-c', 'import sys; sys.stdout.write(' + repr(text) + ')']), text)
        finding = {'priority': 1, 'file': 'x.py', 'line': 3, 'title': 'Example', 'body': 'Evidence'}
        with patch.object(MODULE, 'command', side_effect=[str(len(text)), text]):
            value = MODULE.source_tool('validate_findings', {'findings': [finding]}, ['git'], 'a'*40, 'b'*40)
        self.assertEqual(json.loads(value[0]['text']), {'status': 'valid'})

    def test_private_review_uses_fixed_snapshot_and_checks_base_drift(self):
        import io
        from unittest.mock import MagicMock
        sys.path.insert(0, str(DIRECTORY.parent))
        import immutable_github_source as immutable
        request = self.fixture_request()
        view = {'headRefOid': 'a'*40, 'baseRefOid': 'b'*40, 'baseRefName': 'main'}
        for changed in ['headRefOid', 'baseRefOid', None]:
            observed = dict(view)
            if changed: observed[changed] = 'c'*40
            source = MagicMock(merge_base='b'*40)
            process = MagicMock(stdin=io.BytesIO())
            with self.subTest(changed=changed), patch.dict(MODULE.os.environ, {'FACTORY_HERMES_GITHUB_TRANSPORT': 'composio'}), patch.object(immutable.READ, 'GithubRead'), patch.object(immutable.READ, 'read', side_effect=[view, observed]), patch.object(immutable, 'Source', return_value=source) as snapshot, patch.object(MODULE, 'command', return_value='') as command, patch.object(MODULE.subprocess, 'Popen', return_value=process), patch.object(MODULE, 'relay_line', return_value=json.dumps({'assessment': {'verdict': 'passed', 'findings': []}}).encode()):
                result = MODULE.run(request)
            self.assertEqual(result['verdict'], 'passed' if changed is None else 'inconclusive')
            self.assertEqual(snapshot.call_args.args[:3], ('example/demo', 'b'*40, 'a'*40))
            self.assertFalse(any('fetch' in call.args[0] for call in command.call_args_list))

    def test_private_source_tools_never_expose_credentials_or_follow_symlinks(self):
        from unittest.mock import MagicMock
        source = MagicMock()
        source.read_file.return_value = 'committed source\n'
        response = MODULE.source_tool('read_file', {'path': 'app.py'}, ['git'], 'a'*40, 'b'*40, source)
        self.assertEqual(json.loads(response[0]['text']), 'committed source\n')
        source.read_file.assert_called_once_with('app.py')
        with self.assertRaises(ValueError):
            MODULE.source_tool('read_file', {'path': 'app.py', 'credential': 'please'}, ['git'], 'a'*40, 'b'*40, source)
