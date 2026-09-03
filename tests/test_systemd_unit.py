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


if __name__ == "__main__":
    unittest.main()
