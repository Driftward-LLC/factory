#!/usr/bin/python3
"""Forced SSH command; this account cannot select a shell or deployment target."""
import os
import re
import sys

command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
if command != 'status' and not re.fullmatch(r'deploy [1-9][0-9]{0,19} [0-9a-f]{40}', command):
    sys.exit('Only a verified Factory deployment request is allowed.')
os.execv('/usr/bin/sudo', ['sudo', '-n', '/usr/local/libexec/factory-auto-deploy', command])
