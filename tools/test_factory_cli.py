import importlib.util
import json
from pathlib import Path
import tempfile
import socketserver
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('canonical_cli', Path(__file__).resolve().parents[1] / 'deploy/factory-cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CanonicalCliTests(unittest.TestCase):
    def test_operator_round_trip_uses_private_parsed_credentials_and_cleans_up(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'deploy'))
        received = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = []
                while True:
                    line = self.rfile.readline()
                    if line == b'\r\n' or not line:
                        break
                    request.append(line.decode().strip())
                received.extend(request)
                body = b'{"attempts":[]}'
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'env').mkdir()
            credentials = path / 'env/factory-operator.env'
            token = 'literal-$(must-not-execute)-token'
            credentials.write_text("HATCHET_FACTORY_OPERATOR_TOKEN='" + token + "'\n")
            credentials.chmod(0o600)
            install = SimpleNamespace(directory=path)
            config = {'installation': str(path / 'installation'), 'production_bundle': str(root / 'deploy')}
            production = cli.production_module(config)
            socket = path / 'operator.sock'
            with socketserver.UnixStreamServer(str(socket), Handler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                original_run = subprocess.run
                calls = []
                def run(args, **kwargs):
                    calls.append((args, kwargs))
                    if args[0] == 'docker':
                        kwargs['stdout'].write((root / 'factory/factory').read_bytes())
                        return SimpleNamespace(returncode=0)
                    with self.assertRaisesRegex(ValueError, 'another operation'):
                        with production.portable.installation_lock(Path(config['installation'])):
                            pass
                    result = original_run(args, **kwargs, capture_output=True, text=True, timeout=10)
                    self.assertNotIn(token, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), '{"attempts":[]}')
                    return result
                try:
                    with patch.object(cli, 'installation', return_value=install), \
                         patch.object(cli, 'operator', return_value=('cid', socket, {})), \
                         patch.object(cli, 'cli_image', return_value='sha256:exact'), \
                         patch.object(cli.subprocess, 'run', side_effect=run):
                        self.assertEqual(cli.dispatch(config, ['attempts', 'list', 'ENG-162', '--json']), 0)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertIn('GET /v1/attempts/work-item/ENG-162 HTTP/1.1', received)
            self.assertIn('Authorization: Bearer ' + token, received)
            self.assertFalse(Path(calls[-1][1]['env']['FACTORY_OPERATOR_ENV']).exists())
            self.assertIn('--network', calls[0][0])
            self.assertIn('--read-only', calls[0][0])

    def test_binding_requires_private_explicit_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binding.json'
            value = dict(installation='/srv/factory', installation_id='identity', production_bundle='/srv/bundle')
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            self.assertEqual(cli.checked_config(path), value)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                cli.checked_config(path)
            path.chmod(0o600)
            value['installation'] = 'relative'
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                cli.checked_config(path)

    def test_logs_are_scoped_and_preserve_arguments(self):
        install = SimpleNamespace(compose=unittest.mock.Mock())
        with patch.object(cli, 'installation', return_value=install):
            cli.dispatch({}, ['logs', 'intake', '-n', '12', '-f'])
            install.compose.assert_called_once_with('logs', '--tail', '12', '--follow', 'intake-worker')
            with self.assertRaises(ValueError):
                cli.dispatch({}, ['logs', '--project-name', 'legacy'])

    def test_mutating_mode_and_legacy_doctor_fail_closed(self):
        with patch.object(cli, 'installation', return_value=SimpleNamespace()):
            for args in (['mode', 'real', '--yes'], ['doctor', 'hatchet']):
                with self.assertRaises(ValueError):
                    cli.dispatch({}, args)

    def test_operator_refuses_wrong_identity_image_or_socket(self):
        install = SimpleNamespace(project='factory-current', metadata={'installation_id': 'identity'},
                                  values={'FACTORY_IMAGE': 'sha256:active'},
                                  compose=lambda *args, **kwargs: 'container\n')
        row = {'Config': {'Labels': {'com.docker.compose.project': install.project,
               'com.docker.compose.service': 'factory-operator', 'io.driftward.factory.installation': 'identity'}},
               'Image': 'sha256:active', 'State': {'Running': True}, 'Mounts': [
                   {'Destination': '/run/driftward-factory-operator', 'Name': 'factory-current-operator-socket',
                    'Type': 'volume', 'Source': '/private/socket'}]}
        with patch.object(cli.subprocess, 'run', side_effect=lambda *a, **k: SimpleNamespace(stdout=json.dumps([row]))):
            self.assertEqual(cli.operator(install)[1], Path('/private/socket/operator.sock'))
            row['Image'] = 'sha256:stale'
            with self.assertRaises(ValueError):
                cli.operator(install)

    def test_cli_source_is_exact_release_image(self):
        image = 'sha256:' + 'a' * 64
        install = SimpleNamespace(images={'factory': image})
        row = {'Id': image, 'Config': {'Labels': {
            'org.opencontainers.image.source': 'https://github.com/Driftward-LLC/factory',
            'org.opencontainers.image.revision': 'b' * 40}}}
        with patch.object(cli.subprocess, 'run', side_effect=lambda *a, **k: SimpleNamespace(stdout=json.dumps([row]))):
            self.assertEqual(cli.cli_image(install), image)
            row['Config']['Labels']['org.opencontainers.image.source'] = 'other'
            with self.assertRaises(ValueError):
                cli.cli_image(install)

    def test_operator_lock_precedes_loading_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'installation': str(Path(directory) / 'installation'),
                      'production_bundle': str(Path(__file__).resolve().parents[1] / 'deploy')}
            production = cli.production_module(config)
            with production.portable.installation_lock(Path(config['installation'])):
                with patch.object(cli, 'installation') as construction:
                    with self.assertRaisesRegex(ValueError, 'another operation'):
                        cli.dispatch(config, ['attempts', 'active', '--json'])
                    construction.assert_not_called()


if __name__ == '__main__':
    unittest.main()
