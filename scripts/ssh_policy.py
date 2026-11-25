"""Compare effective SSH directives without relying on dump keyword capitalization."""

def require_restricted(effective, coordinator_port):
    required = {'allowtcpforwarding': 'local', 'allowstreamlocalforwarding': 'no',
                'maxsessions': '0', 'permittty': 'no', 'permitlisten': 'none',
                'allowagentforwarding': 'no', 'x11forwarding': 'no',
                'passwordauthentication': 'no', 'permituserrc': 'no',
                'permitopen': f'127.0.0.1:{coordinator_port}'}
    seen = {}
    for line in effective.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        key, value = fields[0].casefold(), fields[1]
        if key in required:
            if key in seen:
                raise ValueError('Duplicate effective SSH directive: ' + key)
            seen[key] = value
    if seen != required:
        mismatches = sorted(k for k, v in required.items() if seen.get(k) != v)
        raise ValueError('Effective SSH restrictions differ: ' + ', '.join(mismatches))
