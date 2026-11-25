#!/usr/bin/env python3
import unittest
from ssh_policy import require_restricted

POLICY = '''AllowTcpForwarding local
AllowStreamLocalForwarding no
MaxSessions 0
PermitTTY no
PermitListen none
AllowAgentForwarding no
X11Forwarding no
PasswordAuthentication no
PermitUserRC no
PermitOpen 127.0.0.1:12345
'''


class EffectivePolicyTests(unittest.TestCase):
    def test_old_lowercase_and_new_mixed_case_keywords(self):
        require_restricted(POLICY, 12345)
        require_restricted(POLICY.lower(), 12345)

    def test_changed_values_missing_fields_and_other_destinations_fail(self):
        for text in (POLICY.replace('Forwarding local', 'Forwarding yes'),
                     POLICY.replace('MaxSessions 0', 'MaxSessions 1'),
                     POLICY.replace('PermitListen none', 'PermitListen any'),
                     POLICY.replace('PermitTTY no\n', ''),
                     POLICY.replace('127.0.0.1:12345', '127.0.0.1:22'),
                     POLICY.replace('127.0.0.1:12345', '127.0.0.1:12345 127.0.0.1:22')):
            with self.subTest(policy=text), self.assertRaises(ValueError):
                require_restricted(text, 12345)

    def test_duplicate_directives_fail(self):
        with self.assertRaises(ValueError):
            require_restricted(POLICY + 'permittty no\n', 12345)


if __name__ == '__main__':
    unittest.main()
