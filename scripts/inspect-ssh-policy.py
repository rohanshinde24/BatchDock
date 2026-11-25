#!/usr/bin/env python3
"""Print only non-secret SSH policy fields when a hosted harness check fails."""
from pathlib import Path
import subprocess
import sys

config = Path(sys.argv[1]) / 'ssh/sshd_config'
version = subprocess.run(['/usr/sbin/sshd', '-V'], capture_output=True, text=True)
print((version.stdout + version.stderr).strip())
policy = subprocess.check_output(['/usr/sbin/sshd', '-T', '-f', str(config.resolve())], text=True)
fields = {'allowtcpforwarding', 'allowstreamlocalforwarding', 'maxsessions', 'permittty',
          'permitlisten', 'allowagentforwarding', 'x11forwarding', 'passwordauthentication',
          'permituserrc', 'permitopen', 'kbdinteractiveauthentication', 'usepam', 'permittunnel'}
for line in policy.splitlines():
    if line.split()[0] in fields:
        print(line)
