#!/usr/bin/env python3
"""Observe the installation's running Hermes runtime; never start or promote it."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

GUARD = """import hashlib
print('sha256:'+hashlib.sha256((open('/proc/sys/kernel/random/boot_id').read().strip()+':'+open('/proc/1/stat').read().rsplit(')',1)[1].split()[19]).encode()).hexdigest())
"""


def observe(container, installation, run=subprocess.check_output):
    uuid.UUID(installation)
    inspected = json.loads(run(['docker', 'inspect', '--type=container', container], timeout=10))[0]
    identity, image = inspected['Id'], inspected['Image']
    if not re.fullmatch('[a-f0-9]{64}', identity) or not re.fullmatch('sha256:[a-f0-9]{64}', image):
        raise ValueError('Docker returned an invalid runtime identity')
    if inspected['Config']['Labels'].get('io.factory.installation') != installation:
        raise ValueError('Container belongs to another installation')
    if inspected['Config']['Labels'].get('com.docker.compose.service') != 'factory-hermes':
        raise ValueError('Container is not the Factory Hermes service')
    if inspected['Config']['Hostname'] != identity[:12] or not inspected['State']['Running']:
        raise ValueError('Runtime must be running with its default Docker hostname')
    incarnation = run(['docker', 'exec', '--user', '10000', identity,
                       'python3', '-c', GUARD], timeout=10).decode().strip()
    if not re.fullmatch('sha256:[a-f0-9]{64}', incarnation):
        raise ValueError('Invalid observed incarnation')
    return {'installation_id': installation, 'runtime': {'schema': 1, 'backend': 'hermes',
            'kind': 'docker', 'instance_id': identity, 'image_digest': image, 'incarnation': incarnation}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--installation-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = observe(args.container, args.installation_id)
    # Mount the parent directory read-only so atomic replacement is visible in Hermes.
    descriptor, temporary = tempfile.mkstemp(dir=args.output.parent, prefix='.runtime-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)  # Public identity only; readable by uid 10000.
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    main()
