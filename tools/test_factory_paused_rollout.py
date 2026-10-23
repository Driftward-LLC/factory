#!/usr/bin/env python3
"""Manual real-Compose check; two cached images with /bin/sh, no host mounts or ports."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old_image')
    parser.add_argument('new_image')
    args = parser.parse_args()
    prod = load('paused_production', 'factory-production.py')
    auto = load('paused_auto', 'factory-auto-deploy.py')
    def docker(*values):
        return subprocess.check_output(['docker', *values], text=True, stderr=subprocess.PIPE).strip()
    images = [json.loads(docker('image', 'inspect', image))[0]['Id'] for image in (args.old_image, args.new_image)]
    assert images[0] != images[1]
    project = 'factory-paused-check-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=project + '-') as temporary:
        configuration = Path(temporary) / 'compose.json'
        identity = str(uuid.uuid4())
        labels = {prod.state.IDENTITY_LABEL: identity, prod.TENANT_LABEL: 'disposable-check'}
        def configure(image):
            service = {'image': image, 'entrypoint': ['/bin/sh', '-c'], 'command': ['while true; do sleep 1; done'],
                       'network_mode': 'none', 'read_only': True, 'cap_drop': ['ALL'], 'labels': labels}
            configuration.write_text(json.dumps({'services': {'bridge': service, 'bridge-executor-worker': service}}))
        class Installation:
            services = ('bridge', 'bridge-executor-worker')
            def compose(self, *values, private=True):
                return docker('compose', '-p', project, '-f', str(configuration), *values)
            def service_rows(self):
                raw = self.compose('ps', '--all', '--format', 'json')
                return [json.loads(line) for line in raw.splitlines()]
            def preflight(self):
                prod.Production.assert_candidate_writers(self)
        install = Installation()
        install.project, install.metadata, install.tenant = project, {'installation_id': identity}, 'disposable-check'
        install.env, install.images, install.release_images = dict(os.environ), {'factory': images[0]}, [images[0]]
        configure(images[0])
        try:
            install.compose('up', '-d', '--no-build', '--pull', 'never')
            install.compose('stop', 'bridge-executor-worker')
            # Production cold backup has stopped every service before the pin changes.
            install.compose('stop', 'bridge')
            configure(images[1])
            install.images, install.release_images = {'factory': images[1]}, images
            # The actual production gate accepts an exited historical image.
            install.preflight()
            auto.resume(install, ('bridge-executor-worker',))
            rows = {row['Service']: row for row in install.service_rows()}
            executor = json.loads(docker('container', 'inspect', rows['bridge-executor-worker']['ID']))[0]
            assert executor['State']['Status'] == 'exited' and executor['Image'] == images[0]
            bridge = json.loads(docker('container', 'inspect', rows['bridge']['ID']))[0]
            assert bridge['State']['Status'] == 'running' and bridge['Image'] == images[1]
            # Guarded resume recreates only that paused consumer at the new pin.
            install.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'bridge-executor-worker')
            auto.healthy(install)
            rows = {row['Service']: row for row in install.service_rows()}
            executor = json.loads(docker('container', 'inspect', rows['bridge-executor-worker']['ID']))[0]
            assert executor['State']['Status'] == 'running' and executor['Image'] == images[1]
            print(json.dumps({'result': 'passed', 'project': project, 'mounts': 0, 'ports': 0,
                              'paused_old_image_preserved': True, 'resume_recreated_exact_image': True}))
        finally:
            install.compose('down', '--volumes', '--remove-orphans')
        assert not docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project)


if __name__ == '__main__':
    main()
