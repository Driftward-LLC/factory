#!/usr/bin/env python3
"""Ensure installation-local native Hermes role profiles without changing auth."""
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

PROFILES = {
    'factory-executor': 'Factory implementation and fixes.',
    'factory-independent-reviewer': 'Factory independent review.',
}


def reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
            if normalized.endswith(('apikey', 'token', 'password', 'secret', 'authorization')) and item:
                raise ValueError('native model seed contains inline credentials')
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)


def ensure_profiles():
    from hermes_constants import get_hermes_home, get_default_hermes_root
    from hermes_cli.config import read_user_config_raw
    from hermes_cli.profiles import list_profile_names, get_active_profile_name, launch_model_seed
    home = get_hermes_home()
    if home.resolve() != get_default_hermes_root().resolve():
        raise ValueError('bootstrap requires the installation Hermes root')
    os.umask(0o077)
    # Serialize startup and explicit repair without changing Hermes's active profile.
    fd = os.open(home / '.factory-default-profiles.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = set(list_profile_names())
        active = get_active_profile_name()
        missing = set(PROFILES) - before
        for name in missing:
            path = home / 'profiles' / name
            if path.exists() or path.is_symlink():
                raise ValueError('unrecognized existing role profile directory')
        source = home / 'config.yaml'
        if missing and source.is_file():
            reject_credentials(launch_model_seed(read_user_config_raw(source)))
        for name, description in PROFILES.items():
            if name not in missing:
                continue
            subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'profile', 'create', name,
                            '--no-alias', '--description', description], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not set(PROFILES).issubset(list_profile_names()) or get_active_profile_name() != active:
            raise ValueError('native profile creation verification failed')
        if os.environ.get("FACTORY_HERMES_ENGINEERING_MANIFEST"):
            environment = {**os.environ, "HERMES_HOME": str(home/"profiles/factory-executor")}
            subprocess.run([sys.executable, str(Path(__file__).with_name("executor-readiness.py"))],
                           env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Factory native role profiles ready; authentication and execution gates unchanged.')


if __name__ == '__main__':
    try:
        ensure_profiles()
    except ValueError as error:
        if str(error) == 'native model seed contains inline credentials':
            print('Factory native profile bootstrap refused an inline credential in the native model seed; configure installation authentication through Hermes instead.', file=sys.stderr)
        else:
            print('Factory native profile bootstrap failed; private output withheld.', file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print('Factory native profile bootstrap failed; private output withheld.', file=sys.stderr)
        raise SystemExit(1)
    if len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
