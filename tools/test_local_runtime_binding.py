import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('local_binding', Path(__file__).resolve().parents[1] / 'deploy/local-runtime-binding.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
INSTALLATION = '00000000-0000-4000-8000-000000000001'


class RuntimeBindingTest(unittest.TestCase):
    def observe(self, change=None):
        container = {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64,
                     'Config': {'Hostname': 'a' * 12, 'Labels': {'io.factory.installation': INSTALLATION,
                                'com.docker.compose.service': 'factory-hermes'}}, 'State': {'Running': True}}
        if change:
            change(container)
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[1] == 'inspect':
                return json.dumps([container]).encode()
            self.assertEqual(args[:5], ['docker', 'exec', '--user', '10000', 'a' * 64])
            return ('sha256:' + 'c' * 64).encode()
        return MODULE.observe('service-name', INSTALLATION, run), calls

    def test_pins_inspected_full_id_for_incarnation(self):
        value, calls = self.observe()
        self.assertEqual(value['installation_id'], INSTALLATION)
        self.assertEqual(value['runtime']['instance_id'], 'a' * 64)
        self.assertEqual(len(calls), 2)

    def test_refuses_wrong_installation_service_or_hostname(self):
        mutations = [lambda c: c['Config']['Labels'].update({'io.factory.installation': 'other'}),
                     lambda c: c['Config']['Labels'].update({'com.docker.compose.service': 'bridge'}),
                     lambda c: c['Config'].update(Hostname='custom'),
                     lambda c: c['State'].update(Running=False)]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe(change)
