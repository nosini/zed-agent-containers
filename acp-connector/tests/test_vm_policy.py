"""Check the actual polkit rule, ACL probes and failed-migration rollback."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent.parent
DOMAIN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SOURCE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install-agent-vm")
probe = load("agent-vm-check-acl")
server = load("agent-vm-server")


class PolicyTest(unittest.TestCase):
    def evaluate(self, cases, legacy=False):
        javascript = '''
const vm = require('node:vm');
let rule;
vm.runInNewContext(JSON.parse(process.argv[1]), {
    polkit: {Result: {YES: 'yes', NO: 'no'}, addRule: fn => { rule = fn; }}
});
const cases = JSON.parse(process.argv[2]);
const result = cases.map(c => rule({id: c.id, lookup: key => c[key]},
                                  {user: c.user || 'acp-agent-vm'}) ?? null);
process.stdout.write(JSON.stringify(result));
'''
        result = subprocess.run(["node", "-e", javascript,
                                 json.dumps(installer.policy(SOURCE, DOMAIN, legacy)), json.dumps(cases)],
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_helper_permissions_are_scoped_by_driver_uuid_and_operation(self):
        cases = [{"id": "org.libvirt.unix.manage"}]
        expected = ["yes"]
        for driver in ("QEMU", "network", None):
            for domain in (DOMAIN, OTHER, None):
                for operation in ("getattr", "read", "start", "init-control", "stop", "write",
                                  "delete", "read-secure", "open-namespace", "snapshot", "reset"):
                    cases.append({"id": "org.libvirt.api.domain." + operation,
                                  "connect_driver": driver, "domain_uuid": domain})
                    expected.append("yes" if driver == "QEMU" and domain == DOMAIN and operation in
                                    ("getattr", "read", "start", "init-control") else "no")
        for action in ("org.libvirt.api.connect.search-domains", "org.libvirt.api.connect.write",
                       "org.libvirt.api.network.start", "org.libvirt.api.secret.read",
                       "org.freedesktop.systemd1.manage-units"):
            cases.append({"id": action, "connect_driver": "QEMU", "domain_uuid": DOMAIN})
            expected.append("no")
        cases.append({"id": "org.libvirt.api.connect.getattr", "connect_driver": "QEMU"})
        expected.append("yes")
        self.assertEqual(self.evaluate(cases, legacy=True), expected)

    def test_existing_permissions_are_preserved_only_during_default_acl_migration(self):
        cases = [{"user": "desktop", "id": "org.libvirt.api.domain.write", "connect_driver": "QEMU"},
                 {"user": "desktop", "id": "org.libvirt.unix.manage"},
                 {"user": "desktop", "id": "org.libvirt.api.network.start", "connect_driver": "network"}]
        self.assertEqual(self.evaluate(cases, legacy=True), ["yes", None, None])
        self.assertEqual(self.evaluate(cases, legacy=False), [None, None, None])

    def test_config_edit_preserves_other_options_and_rejects_ambiguous_acl(self):
        for setting in ('#access_drivers = ["polkit"]\n', 'access_drivers = []\n',
                        'access_drivers = ["none"] # previous default\n'):
            text = 'auth_unix_rw = "polkit"\n' + setting + 'log_level = 3\n'
            edited, changed = installer.acl_config(text)
            self.assertTrue(changed)
            self.assertIn('auth_unix_rw = "polkit"', edited)
            self.assertIn('log_level = 3', edited)
            self.assertEqual(installer.acl_config(edited), (edited, False))
        text = 'access_drivers = [\n "polkit", # supported ACL\n]\n'
        self.assertEqual(installer.acl_config(text), (text, False))
        for text in ('access_drivers = ["custom"]\n', 'access_drivers = "polkit"\n',
                     'access_drivers = []\naccess_drivers = ["polkit"]\n'):
            with self.assertRaises(ValueError):
                installer.acl_config(text)

    def test_user_owned_or_symlink_policy_files_are_not_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "configuration"
            path.write_text("original")
            with self.assertRaises(ValueError):  # This fixture is owned by our non-root UID.
                installer.read_owned(path)
            link = Path(temp) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                installer.write_owned(link, "replacement")
            self.assertEqual(path.read_text(), "original")

    def test_domain_operations_use_only_the_private_qemu_socket(self):
        domain = mock.Mock()
        domain.state.return_value = (1, 0)
        domain.ID.return_value = 12
        connection = mock.Mock()
        connection.lookupByUUIDString.return_value = domain
        libvirt = types.SimpleNamespace(open=mock.Mock(return_value=connection))
        with mock.patch.dict(sys.modules, {"libvirt": libvirt}):
            self.assertEqual(server.Domain(DOMAIN).call("state"), (1, 12))
        libvirt.open.assert_called_once_with(server.LIBVIRT_URI)
        self.assertIn("/run/acp-agent-vm-private/libvirt.sock", server.LIBVIRT_URI)
        connection.lookupByUUIDString.assert_called_once_with(DOMAIN)
        connection.close.assert_called_once()

    def test_server_refuses_a_root_fallback(self):
        with mock.patch.object(sys, "argv", ["server"]), mock.patch.object(server.os, "geteuid", return_value=0), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                server.main()
        self.assertEqual(result.exception.code, 2)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "virtqemud.conf"
        self.rule = self.root / "policy.rules"
        self.state = self.root / "migration.json"
        self.original = '# existing configuration\nauth_unix_rw = "polkit"\n'
        self.config.write_text(self.original)
        patches = [mock.patch.object(installer, "QEMU_CONFIG", self.config),
                   mock.patch.object(installer, "ACL_RULE", self.rule),
                   mock.patch.object(installer, "ACL_STATE", self.state),
                   mock.patch.object(installer, "read_owned", side_effect=lambda p: Path(p).read_text() if Path(p).exists() else None),
                   mock.patch.object(installer, "write_owned", side_effect=lambda p, text, mode=0o644: Path(p).write_text(text)),
                   mock.patch.object(installer.time, "sleep")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_failed_verification_restores_config_and_removes_connection_grant(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1 if "-u" in argv else 0, stdout="", stderr="denial not enforced")
        with mock.patch.object(installer.subprocess, "run", side_effect=run) as command:
            with self.assertRaisesRegex(RuntimeError, "ACL verification failed"):
                installer.configure_acl(SOURCE, DOMAIN)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertFalse(self.rule.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.config.with_name(self.config.name + ".acp-agent-vm-backup").read_text(), self.original)
        self.assertIn(mock.call(["systemctl", "try-restart", "virtqemud.service"], check=False), command.call_args_list)

    def test_success_and_reinstall_keep_migration_scope_and_backup(self):
        with mock.patch.object(installer.subprocess, "run", side_effect=lambda argv, **kwargs:
                               subprocess.CompletedProcess(argv, 0, stdout="ACL checks passed", stderr="")) as command:
            installer.configure_acl(SOURCE, DOMAIN)
            installer.configure_acl(SOURCE, OTHER)
        self.assertEqual(installer.acl_config(self.config.read_text())[1], False)
        self.assertEqual(json.loads(self.state.read_text()), {"legacy_qemu_access": True})
        self.assertIn(json.dumps(OTHER), self.rule.read_text())
        self.assertNotIn(json.dumps(DOMAIN), self.rule.read_text())
        self.assertIn("if (true &&", self.rule.read_text())
        self.assertEqual(self.config.with_name(self.config.name + ".acp-agent-vm-backup").read_text(), self.original)
        restarts = [call for call in command.call_args_list if call.args[0] == ["systemctl", "restart", "virtqemud.service"]]
        self.assertEqual(len(restarts), 1)

    def test_existing_acl_and_rule_are_preserved_on_failed_verification(self):
        self.config.write_text('access_drivers = ["polkit"]\n')
        previous = installer.policy(SOURCE, DOMAIN, False)
        self.rule.write_text(previous)
        with mock.patch.object(installer.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 1, stdout="", stderr="failed")) as command:
            with self.assertRaises(RuntimeError):
                installer.configure_acl(SOURCE, OTHER)
        self.assertEqual(self.config.read_text(), 'access_drivers = ["polkit"]\n')
        self.assertEqual(self.rule.read_text(), previous)
        self.assertFalse(any(call.args[0][0] == "systemctl" for call in command.call_args_list))


class ProbeTest(unittest.TestCase):
    def test_probe_rejects_unexpected_permissions_or_unrelated_errors(self):
        class Error(Exception):
            def get_error_code(self):
                return self.args[0]
        libvirt = types.SimpleNamespace(libvirtError=Error, VIR_ERR_ACCESS_DENIED=1, VIR_ERR_AUTH_FAILED=2,
                                        VIR_ERR_OPERATION_DENIED=3, VIR_ERR_NO_DOMAIN=4, VIR_DOMAIN_XML_SECURE=1)
        conn, domain = mock.Mock(), mock.Mock()
        conn.lookupByUUIDString.side_effect = lambda value: domain if value == DOMAIN else (_ for _ in ()).throw(Error(1))
        conn.listAllDomains.side_effect = Error(1)
        domain.XMLDesc.side_effect = Error(1)
        domain.setAutostart.side_effect = Error(1)
        probe.check(conn, libvirt, DOMAIN, OTHER)
        domain.setAutostart.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "autostart was allowed"):
            probe.check(conn, libvirt, DOMAIN)
        conn.listAllDomains.side_effect = Error(999)
        with self.assertRaisesRegex(RuntimeError, "unexpected libvirt error"):
            probe.check(conn, libvirt, DOMAIN)


if __name__ == "__main__":
    unittest.main()
