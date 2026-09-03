import unittest

from debbuilder.systemd_unit import generate_unit


class SystemdUnitTests(unittest.TestCase):
    def test_generates_configured_directives_and_omits_empty_ones(self):
        unit = generate_unit({
            "enabled": True, "description": "Demo service", "after": ["network.target"],
            "wants": [], "requires": [], "type": "simple", "user": "svc-user",
            "group": "svc-group", "environment_files": ["-/etc/demo/env"],
            "environment": {"MODE": "production"}, "exec_start_pre": ["/bin/true"],
            "command": "/opt/demo/bin/demo --serve", "exec_start_post": [], "exec_stop": [],
            "restart": "on-failure", "restart_sec": "", "timeout_start_sec": "30",
            "timeout_stop_sec": "", "kill_signal": "", "standard_output": "journal",
            "standard_error": "",
        })
        self.assertIn("User=svc-user", unit)
        self.assertIn("Group=svc-group", unit)
        self.assertIn("ExecStart=/opt/demo/bin/demo --serve", unit)
        self.assertIn('Environment="MODE=production"', unit)
        self.assertIn("TimeoutStartSec=30", unit)
        self.assertNotIn("RestartSec=", unit)
        self.assertNotIn("StandardError=", unit)

    def test_rejects_multiline_values(self):
        with self.assertRaisesRegex(ValueError, "single-line"):
            generate_unit({"enabled": True, "description": "bad\nvalue", "command": "/bin/true"})

    def test_unit_generation_does_not_depend_on_boot_enablement(self):
        unit = generate_unit({"enabled": False, "name": "demo.service", "command": "/bin/true"})
        self.assertIn("ExecStart=/bin/true", unit)

    def test_advanced_directives_are_rendered_and_lists_are_repeated(self):
        unit = generate_unit({
            "description": "Mail", "conflicts": ["postfix.service", "exim4.service"], "after": ["network-online.target"],
            "type": "simple", "user": "mail", "group": "mail", "command": "/usr/bin/mail",
            "limit_nofile": "65536", "kill_mode": "process", "kill_signal": "SIGINT", "restart": "on-failure", "restart_sec": "5",
            "syslog_identifier": "mail", "ambient_capabilities": ["CAP_NET_BIND_SERVICE"],
        })
        self.assertIn("Conflicts=postfix.service\nConflicts=exim4.service", unit)
        self.assertIn("LimitNOFILE=65536", unit)
        self.assertIn("KillMode=process", unit)
        self.assertIn("KillSignal=SIGINT", unit)
        self.assertIn("SyslogIdentifier=mail", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", unit)

    def test_absent_advanced_directives_are_omitted(self):
        unit = generate_unit({"command": "/bin/true"})
        for directive in ("Conflicts=", "LimitNOFILE=", "KillMode=", "SyslogIdentifier=", "AmbientCapabilities="):
            self.assertNotIn(directive, unit)


if __name__ == "__main__":
    unittest.main()
