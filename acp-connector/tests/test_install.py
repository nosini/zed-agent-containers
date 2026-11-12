"""Exercise installation ordering with fake VM setup and systemd commands."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent.parent


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.source = self.repo / "acp-connector"
        self.source.mkdir(parents=True)
        self.vm = self.root / "vm-sockets"
        self.vm.mkdir()
        # Replace only the fixed runtime directory in the copied installers.
        # No actual container runtime, host home, /run or systemd is touched.
        for name in ("install.sh", "agent-vm-client.py", "container-policy.py", "acp-broker.py", "acp-client.py",
                     "agent-environment.md", "agent-home.volume", "codex-acp-connector.socket",
                     "codex-acp-connector.service", "claude-acp-connector.socket", "claude-acp-connector.service",
                     "claude-b-acp-connector.socket", "claude-b-acp-connector.service",
                     "codex-teams-acp-connector.socket", "codex-teams-acp-connector.service",
                     "claude-teams-acp-connector.socket", "claude-teams-acp-connector.service",
                     "opencode-acp-connector.socket", "opencode-acp-connector.service",
                     "opencode-teams-acp-connector.socket", "opencode-teams-acp-connector.service",
                     "teams-mcp-relay.py"):
            text = (SOURCE / name).read_text()
            if name == "install.sh":
                text = text.replace("/run/acp-agent-vm", str(self.vm))
            (self.source / name).write_text(text)
        (self.source / "install.sh").chmod(0o755)
        (self.repo / "install.sh").write_text((SOURCE.parent / "install.sh").read_text().replace(
            "/run/acp-agent-vm", str(self.vm)))
        for profile in ("codex", "claude", "opencode"):
            directory = self.repo / (profile + "-container")
            directory.mkdir()
            shutil.copy(SOURCE.parent / (profile + "-container") / (profile + "-agent-state.volume"), directory)
        shutil.copy(SOURCE.parent / "claude-container/claude-b-agent-state.volume", self.repo / "claude-container")

        self.home = self.root / "home"
        self.settings = self.home / ".config/acp-connector"
        self.settings.mkdir(parents=True)
        self.options = self.settings / "broker.env"
        self.old_options = f'ACP_BROKER_OPTS="--memory {self.root}/memory"\n'
        self.options.write_text(self.old_options)
        (self.settings / "gitconfig").touch()
        shutil.copy(SOURCE / "agent-environment.md", self.settings)
        self.installed = self.home / ".local/libexec/acp-connector/acp-broker.py"
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text("previous broker")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.events = self.root / "events"
        self.events.touch()
        for name, script in {
            "python3": 'printf "configure\\n" >> "$INSTALL_EVENTS"\nexit "$VM_SETUP_RESULT"\n',
            "systemctl": 'printf "%s\\n" "$*" >> "$INSTALL_EVENTS"\n',
        }.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\n" + script)
            path.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "INSTALL_EVENTS": str(self.events), "GIT_CONFIG_GLOBAL": str(self.settings / "gitconfig"),
                    "VM_SETUP_RESULT": "0"}

    def run_install(self, result, *flags, top=False):
        script = self.repo / "install.sh" if top else self.source / "install.sh"
        return subprocess.run(["sh", str(script), *flags], env={**self.env, "VM_SETUP_RESULT": str(result)},
                              cwd=self.repo, text=True, capture_output=True, timeout=10)

    def test_failed_vm_configuration_leaves_installed_broker_intact(self):
        result = self.run_install(7, "--agent-vm")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.options.read_text(), self.old_options)
        self.assertEqual(self.installed.read_text(), "previous broker")
        self.assertEqual(self.events.read_text().splitlines(), ["configure"])
        # A later retry installs and restarts normally, without manual repair.
        result = self.run_install(0, "--agent-vm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.installed.read_text(), "previous broker")
        self.assertTrue((self.installed.parent / "container-policy.py").is_file())
        self.assertIn("--vm-socket-dir " + str(self.vm), self.options.read_text())
        self.assertEqual(self.events.read_text().splitlines()[-8:], ["--user daemon-reload",
                         "--user try-restart codex-acp-connector.service",
                         "--user try-restart claude-acp-connector.service",
                         "--user try-restart claude-b-acp-connector.service",
                         "--user try-restart opencode-acp-connector.service",
                         "--user try-restart codex-teams-acp-connector.service",
                         "--user try-restart claude-teams-acp-connector.service",
                         "--user try-restart opencode-teams-acp-connector.service"])
        # Teams stays opt-in: units are installed but nothing enables them.
        units = self.home / ".config/systemd/user"
        self.assertTrue((units / "codex-teams-acp-connector.socket").is_file())
        self.assertTrue((self.installed.parent / "teams-mcp-relay.py").is_file())
        self.assertNotIn("teams-acp-connector.socket", self.events.read_text())

    def test_boolean_and_legacy_flags_follow_the_same_setup(self):
        for flags in (("--agent-vm",), ("--vm-socket-dir", str(self.vm))):
            with self.subTest(flags=flags):
                result = self.run_install(0, "--no-build", "--no-test", *flags, top=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--user enable --now codex-acp-connector.socket claude-acp-connector.socket "
                              "opencode-acp-connector.socket", self.events.read_text())
                self.assertIn("--vm-socket-dir " + str(self.vm), self.options.read_text())


if __name__ == "__main__":
    unittest.main()
