"""Real Unix/TCP transports with a fake hypervisor; no host VM privileges."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE.parent / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load("agent-vm-server")
client = load("agent-vm-client")
broker = load("acp-broker")
policy = load("container-policy")
CLIENT_IDENTIFICATION = b"SSH-2.0-test-client\r\n"
SERVER_IDENTIFICATION = b"SSH-2.0-test-server\r\n"


class FakeDomain:
    def __init__(self, running=False):
        self.state = 1 if running else 5
        self.runtime_id = 10 if running else -1
        self.starts = self.shutdowns = 0
        self.refuse_shutdown = False
        self.delay_shutdown = False
        self.error = False
        self.shutdown_error = False
        self.state_error_after_shutdown = False
        self.calls = []

    def call(self, operation):
        self.calls.append(operation)
        if self.error:
            raise RuntimeError("hypervisor unavailable")
        if operation == "state":
            if self.state_error_after_shutdown and self.shutdowns:
                raise RuntimeError("state unavailable after shutdown")
            return self.state, self.runtime_id
        if operation == "start":
            self.starts += 1
            self.state, self.runtime_id = 1, 10 + self.starts
            return self.runtime_id
        if operation == "shutdown":
            self.shutdowns += 1
            if self.shutdown_error:
                raise RuntimeError("shutdown request failed")
            if not self.refuse_shutdown:
                self.state = 4 if self.delay_shutdown else 5
                if self.state == 5:
                    self.runtime_id = -1


class RelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.domain = FakeDomain()
        self.relay = server.Relay(self.domain, "127.0.0.1", 1, self.root / "holds.json", os.getuid(),
                                  idle_seconds=0.01, boot_seconds=3, shutdown_seconds=0.02)
        self.tasks = set()
        self.servers = []
        self.writers = []
        self.ssh_path = str(self.root / "ssh.sock")
        self.control_path = str(self.root / "control.sock")

        def track(callback):
            async def handler(reader, writer):
                task = asyncio.current_task()
                self.tasks.add(task)
                try:
                    await callback(reader, writer)
                finally:
                    self.tasks.discard(task)
            return handler
        self.track = track
        self.servers.append(await asyncio.start_unix_server(track(self.relay.ssh), self.ssh_path))
        self.servers.append(await asyncio.start_unix_server(track(self.relay.control), self.control_path, limit=4096))

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        for instance in self.servers:
            instance.close()
            await instance.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def tcp(self, callback=None, handshake=True):
        async def echo(reader, writer):
            try:
                data = await reader.read()
                writer.write(b"reply:" + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        async def identify(reader, writer):
            self.assertEqual(await reader.readline(), CLIENT_IDENTIFICATION)
            writer.write(SERVER_IDENTIFICATION)
            await writer.drain()
            await (callback or echo)(reader, writer)
        instance = await asyncio.start_server(self.track(identify if handshake else callback), "127.0.0.1", 0)
        self.servers.append(instance)
        self.relay.port = instance.sockets[0].getsockname()[1]

    async def ssh(self, expect_banner=True, send_identification=True):
        reader, writer = await asyncio.open_unix_connection(self.ssh_path)
        self.writers.append(writer)
        if send_identification:
            writer.write(CLIENT_IDENTIFICATION)
            await writer.drain()
        if expect_banner:
            self.assertEqual(await asyncio.wait_for(reader.readline(), 3), SERVER_IDENTIFICATION)
        return reader, writer

    async def control(self, request):
        reader, writer = await asyncio.open_unix_connection(self.control_path)
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), 3))
        writer.close()
        await writer.wait_closed()
        return result

    async def settle(self):
        async def wait():
            while self.relay.active:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 3)

    async def idle(self):
        self.relay.last_use -= 10
        await self.relay.idle_check()

    async def test_concurrent_cold_start_binary_and_half_close(self):
        await self.tcp()
        payload = bytes(range(256)) * 1000
        connections = await asyncio.gather(*(self.ssh() for _ in range(8)))
        for _, writer in connections:
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
        results = await asyncio.gather(*(asyncio.wait_for(reader.read(), 3) for reader, _ in connections))
        self.assertEqual(results, [b"reply:" + payload] * 8)
        self.assertEqual(self.domain.starts, 1)
        await self.settle()
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 1)
        self.assertEqual(self.domain.state, 5)

    async def test_active_ssh_prevents_idle_shutdown(self):
        await self.tcp()
        reader, writer = await self.ssh()
        writer.write(b"active")
        await writer.drain()
        await asyncio.sleep(0.02)
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 0)
        writer.write_eof()
        self.assertEqual(await reader.read(), b"reply:active")

    async def test_holds_survive_restart_and_release_resets_grace(self):
        await self.relay.ensure_running()
        response = await self.control({"op": "hold", "label": "project/build"})
        token = response["token"]
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 0)
        restarted = server.Relay(self.domain, "127.0.0.1", 22, self.root / "holds.json", os.getuid())
        self.assertEqual(restarted.holds, {token: "project/build"})
        self.assertIsNone(restarted.owned_id, "restart must not claim a running VM")
        response = await self.control({"op": "release", "token": token})
        self.assertTrue(response["ok"])
        await self.relay.idle_check()
        self.assertEqual(self.domain.shutdowns, 0, "release must reset idle timer")
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 1)
        self.assertEqual(json.loads((self.root / "holds.json").read_text()), {})

    async def test_manually_started_or_replaced_vm_is_not_shut_down(self):
        self.domain.state, self.domain.runtime_id = 1, 123
        await self.relay.ensure_running()
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 0)
        self.domain.state, self.domain.runtime_id = 5, -1
        await self.relay.ensure_running()
        self.domain.runtime_id += 1  # User replaced the running instance.
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 0)
        self.assertIsNone(self.relay.owned_id)

    async def test_shutdown_refusal_is_never_forced_or_retried(self):
        await self.relay.ensure_running()
        self.domain.refuse_shutdown = True
        with self.assertLogs("agent-vm", level="ERROR"):
            await self.idle()
        await self.idle()
        self.assertEqual(self.domain.shutdowns, 1)
        self.assertEqual(self.domain.state, 1)
        self.assertIsNone(self.relay.owned_id)

    async def test_reconnect_during_shutdown_waits_then_restarts(self):
        await self.tcp()
        await self.relay.ensure_running()
        self.domain.delay_shutdown = True
        self.relay.shutdown_seconds = 3
        self.relay.last_use -= 10
        idle = asyncio.create_task(self.relay.idle_check())
        while self.domain.state != 4:
            await asyncio.sleep(0.005)
        reader, writer = await self.ssh(expect_banner=False)
        writer.write(b"after-shutdown")
        writer.write_eof()
        await asyncio.sleep(0.02)
        self.assertEqual(self.domain.starts, 1)
        self.domain.state, self.domain.runtime_id = 5, -1
        await idle
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), SERVER_IDENTIFICATION + b"reply:after-shutdown")
        self.assertEqual(self.domain.starts, 2)

    async def test_control_is_bounded_fixed_and_does_not_start_vm(self):
        for request in ({"op": "start", "domain": "other"}, {"op": "hold", "label": "x", "command": "id"},
                        {"op": "release", "token": []}, {"op": "hold", "label": ""}, []):
            self.assertFalse((await self.control(request))["ok"])
        status = await self.control({"op": "status"})
        self.assertEqual(status["holds"], {})
        self.assertEqual(self.domain.starts, 0)
        reader, writer = await asyncio.open_unix_connection(self.control_path)
        writer.write(b"x" * 5000 + b"\n")
        await writer.drain()
        self.assertFalse(json.loads(await reader.readline())["ok"])
        writer.close()
        await writer.wait_closed()

    async def test_different_uid_is_refused(self):
        self.relay.allowed_uid = os.getuid() + 1
        reader, _ = await self.ssh(expect_banner=False, send_identification=False)
        self.assertEqual(await reader.read(), b"")
        self.assertEqual(self.domain.starts, 0)

    async def test_hold_limit_unicode_labels_and_real_control_client(self):
        for _ in range(server.MAX_HOLDS):
            response = await self.control({"op": "hold", "label": "\U0001f527" * 128})
            self.assertTrue(response["ok"])
        self.assertFalse((await self.control({"op": "hold", "label": "overflow"}))["ok"])
        status = await asyncio.to_thread(client.request, self.control_path, {"op": "status"})
        self.assertEqual(len(status["holds"]), server.MAX_HOLDS)

    async def test_boot_timeout_cleans_up_activity(self):
        self.relay.boot_seconds = 0.02
        # Reserve and release a port to get a currently closed endpoint.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.relay.port = probe.getsockname()[1]
        reader, _ = await self.ssh(expect_banner=False)
        with self.assertLogs("agent-vm", level="ERROR"):
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        await self.settle()
        self.assertEqual(self.relay.active + self.relay.controls, 0)

    async def test_cold_boot_retries_early_disconnects_without_closing_client(self):
        attempts = []
        preamble = b"VM ready\r\n"
        packet = b"\x00\xffkey-exchange"

        async def booting(reader, writer):
            try:
                attempts.append(await reader.readline())
                if len(attempts) == 1:
                    return  # TCP accepted while the VM is still booting.
                if len(attempts) == 2:
                    writer.write(b"VM not ready\r\n")
                    return
                if len(attempts) == 3:
                    writer.write(b"SSH-2.0-incomplete")
                    return
                writer.write(preamble + SERVER_IDENTIFICATION + packet)
                await writer.drain()
                writer.write(await reader.read())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        await self.tcp(booting, handshake=False)
        reader, writer = await self.ssh(expect_banner=False)
        writer.write(b"client-payload")
        writer.write_eof()
        response = await asyncio.wait_for(reader.read(), 4)
        self.assertEqual(response, preamble + SERVER_IDENTIFICATION + packet + b"client-payload")
        self.assertEqual(attempts, [CLIENT_IDENTIFICATION] * 4)
        self.assertEqual(self.domain.starts, 1)
        await self.settle()
        self.assertEqual(self.relay.active, 0)

    async def test_silent_listener_obeys_boot_deadline_and_keeps_controls_available(self):
        self.relay.boot_seconds = 0.1
        connected = asyncio.Event()

        async def silent(reader, writer):
            try:
                self.assertEqual(await reader.readline(), CLIENT_IDENTIFICATION)
                connected.set()
                self.assertEqual(await reader.read(), b"")
            finally:
                writer.close()
                await writer.wait_closed()

        await self.tcp(silent, handshake=False)
        started = asyncio.get_running_loop().time()
        reader, _ = await self.ssh(expect_banner=False)
        with self.assertLogs("agent-vm", level="ERROR") as logs:
            await asyncio.wait_for(connected.wait(), 1)
            self.assertTrue((await self.control({"op": "status"}))["ok"])
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertLess(asyncio.get_running_loop().time() - started, 1)
        self.assertIn("boot timeout", logs.output[-1])
        await self.settle()
        self.assertEqual(self.relay.active, 0)

    async def test_shutdown_errors_are_not_retried(self):
        for attribute in ("shutdown_error", "state_error_after_shutdown"):
            with self.subTest(attribute=attribute):
                self.domain = FakeDomain()
                self.relay.domain = self.domain
                await self.relay.ensure_running()
                setattr(self.domain, attribute, True)
                with self.assertRaises(RuntimeError):
                    await self.idle()
                await self.idle()
                self.assertEqual(self.domain.shutdowns, 1)
                self.assertIsNone(self.relay.owned_id)

    async def test_paused_and_suspended_vm_stop_periodic_hypervisor_reads(self):
        for state in (3, 7):
            with self.subTest(state=state):
                self.domain = FakeDomain()
                self.relay.domain = self.domain
                await self.relay.ensure_running()
                self.domain.state = state
                await self.idle()
                count = self.domain.calls.count("state")
                for _ in range(5):
                    await self.idle()
                self.assertEqual(self.domain.calls.count("state"), count)
                self.assertIsNone(self.relay.owned_id)
                self.domain.state = 1
                await self.idle()
                self.assertEqual(self.domain.shutdowns, 0)

    async def test_controls_work_with_all_ssh_slots_used(self):
        async def echo_lines(reader, writer):
            try:
                writer.write(b"ready\n")
                await writer.drain()
                while line := await reader.readline():
                    writer.write(line)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        await self.tcp(echo_lines)
        connections = await asyncio.gather(*(self.ssh() for _ in range(server.MAX_CONNECTIONS)))
        await asyncio.gather(*(reader.readline() for reader, _ in connections))
        self.assertEqual(self.relay.active, server.MAX_CONNECTIONS)
        overflow, _ = await self.ssh(expect_banner=False, send_identification=False)
        self.assertEqual(await overflow.read(), b"")
        status = await self.control({"op": "status"})
        self.assertEqual(status["ssh_connections"], server.MAX_CONNECTIONS)
        token = (await self.control({"op": "hold", "label": "full"}))["token"]
        self.assertTrue((await self.control({"op": "release", "token": token}))["ok"])

    async def test_control_overload_gets_a_structured_reply(self):
        for _ in range(server.MAX_CONTROL_CONNECTIONS):
            _, writer = await asyncio.open_unix_connection(self.control_path)
            self.writers.append(writer)
        async def filled():
            while self.relay.controls != server.MAX_CONTROL_CONNECTIONS:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(filled(), 1)
        response = await self.control({"op": "status"})
        self.assertFalse(response["ok"])
        self.assertIn("busy", response["error"])

    async def test_slow_hold_commit_does_not_block_ssh_or_status(self):
        started, finish = threading.Event(), threading.Event()
        original = self.relay.save_holds
        async def echo_lines(reader, writer):
            try:
                while line := await reader.readline():
                    writer.write(line)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        await self.tcp(echo_lines)
        reader, writer = await self.ssh()
        writer.write(b"first\n")
        self.assertEqual(await reader.readline(), b"first\n")
        def slow_save(holds):
            started.set()
            if not finish.wait(2):
                raise RuntimeError("test commit not released")
            original(holds)
        with mock.patch.object(self.relay, "save_holds", side_effect=slow_save):
            pending = asyncio.create_task(self.control({"op": "hold", "label": "slow"}))
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
                self.assertFalse(pending.done(), "hold must not be acknowledged before fsync")
                writer.write(b"during-commit\n")
                self.assertEqual(await asyncio.wait_for(reader.readline(), 0.5), b"during-commit\n")
                self.assertTrue((await asyncio.wait_for(self.control({"op": "status"}), 0.5))["ok"])
            finally:
                finish.set()
                response = await pending
            self.assertIn(response["token"], self.relay.holds)

    async def test_cancelled_commit_finishes_before_releasing_lock(self):
        started, finish = threading.Event(), threading.Event()
        original = self.relay.save_holds
        def slow_save(holds):
            started.set()
            if not finish.wait(2):
                raise RuntimeError("test commit not released")
            original(holds)
        async def commit():
            async with self.relay.lock:
                await self.relay.commit_holds({"3e04da79-c477-4f96-9bd3-996ef7384ceb": "cancelled"})
        with mock.patch.object(self.relay, "save_holds", side_effect=slow_save):
            pending = asyncio.create_task(commit())
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
                pending.cancel()
                await asyncio.sleep(0.01)
                pending.cancel()
                await asyncio.sleep(0.01)
                self.assertTrue(self.relay.lock.locked())
                self.assertFalse(pending.done())
            finally:
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
            self.assertFalse(self.relay.lock.locked())
            self.assertEqual(self.relay.holds, json.loads((self.root / "holds.json").read_text()))

    async def test_real_proxy_large_full_duplex_and_half_close(self):
        sent = bytes(range(256)) * 16384
        greeting = b"server-first" * 300000
        async def exchange(reader, writer):
            try:
                # Fill the receive direction before reading stdin, exercising
                # backpressure in both the actual client and host relay.
                writer.write(greeting)
                await writer.drain()
                data = await reader.read()
                self.assertEqual(data, sent)
                writer.write(b"done")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        await self.tcp(exchange)
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I",
                                                    str(HERE.parent / "agent-vm-client.py"),
                                                    "--socket-dir", str(self.root), "proxy",
                                                    stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(CLIENT_IDENTIFICATION + sent), 10)
            self.assertEqual(proc.returncode, 0, stderr.decode())
            self.assertEqual(stdout, SERVER_IDENTIFICATION + greeting + b"done")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


class ClientAndConfigTest(unittest.TestCase):
    def test_large_peer_uids_are_authorized(self):
        for uid in (2**31, 2**32 - 2):
            credentials = (123).to_bytes(4, sys.byteorder, signed=True) + uid.to_bytes(4, sys.byteorder) * 2
            writer = mock.Mock()
            writer.get_extra_info.return_value.getsockopt.return_value = credentials
            relay = object.__new__(server.Relay)
            relay.allowed_uid = uid
            self.assertTrue(relay.permitted(writer))
            relay.allowed_uid = uid - 1
            self.assertFalse(relay.permitted(writer))
            with mock.patch.object(broker.os, "getuid", return_value=uid):
                self.assertEqual(broker.Broker.peer_pid(None, writer, "test"), 123)
            with mock.patch.object(broker.os, "getuid", return_value=uid - 1):
                self.assertIsNone(broker.Broker.peer_pid(None, writer, "test"))

    def test_stale_and_failed_temp_files_do_not_block_configure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent-vm.conf"
            original = "Host agent-vm\n  HostName 192.0.2.2\n"
            path.write_text(original)
            (Path(temp) / "agent-vm.conf.new").write_text("interrupted old run")
            with mock.patch.object(client.Path, "replace", side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    client.configure(path)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])
            client.configure(path)
            self.assertIn(client.PROXY, path.read_text())
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_configure_timeout_uses_helper_settings_or_legacy_maximum(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent-vm.conf"
            for status, timeout in (({"boot_seconds": 180, "shutdown_seconds": 120}, 360),
                                    ({"boot_seconds": 1800, "shutdown_seconds": 120}, 1980), ({}, 1980)):
                path.write_text("Host agent-vm\n  HostName 192.0.2.2\n")
                with mock.patch.object(sys, "argv", ["client", "configure", "--profile", str(path)]), \
                        mock.patch.object(client.Path, "is_socket", return_value=True), \
                        mock.patch.object(client, "request", return_value={"ok": True, **status}):
                    self.assertEqual(client.main(), 0)
                self.assertIn(f"ConnectTimeout {timeout}", path.read_text())

    def test_vm_mounts_are_rechecked_for_every_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            sockets = Path(temp) / "vm"
            launch = broker.podman_launcher("codex", "/git", vm_socket_dir=str(sockets))
            argv, _ = launch("/git/project", "test")
            self.assertFalse(any("dst=/opt/agent-vm" in item for item in argv))
            sockets.mkdir()
            with mock.patch.object(broker, "VM_CLIENT", str(Path(temp) / "missing-client.py")):
                argv, _ = launch("/git/project", "test")
                self.assertFalse(any("dst=/opt/agent-vm" in item for item in argv))
            argv, _ = launch("/git/project", "test")
            self.assertTrue(any("dst=/opt/agent-vm," in item for item in argv))
            sockets.rmdir()
            argv, _ = launch("/git/project", "test")
            self.assertFalse(any("dst=/opt/agent-vm" in item for item in argv))

    def test_setup_and_broker_use_the_same_container_restrictions(self):
        setup = policy.configure_vm_argv("/client.py", "/sockets")
        agent = broker.podman_argv("codex", "test", "/git", "/git/project")
        for flag in policy.RUN_FLAGS:
            self.assertIn(flag, setup)
            self.assertIn(flag, agent)
        self.assertIn("--network=none", setup)
        self.assertEqual(setup[-3:], ["-I", "/opt/agent-vm-client.py", "configure"])

    def test_configure_preserves_identity_pin_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "vm.conf"
            original = ("Host\tagent-vm\n  HostName 192.0.2.2\n  User agent\n"
                        "  IdentityFile /not-read/private-key\n  IdentitiesOnly yes\n"
                        "  IdentityAgent none\n  StrictHostKeyChecking yes\n"
                        "  UserKnownHostsFile /not-read/pinned-key\n  ProxyCommand=old\n  ConnectTimeout 5\n")
            profile.write_text(original)
            client.configure(profile)
            updated = profile.read_text()
            for line in original.splitlines()[:-2]:
                self.assertIn(line, updated)
            self.assertNotIn("ProxyCommand=old", updated)
            self.assertIn(client.PROXY, updated)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            client.configure(profile)
            self.assertEqual(profile.read_text(), updated)
            output = subprocess.run(["ssh", "-G", "-F", str(profile), "agent-vm"],
                                    capture_output=True, text=True, check=True).stdout
            self.assertIn("hostname 192.0.2.2", output)
            self.assertIn("proxycommand " + client.PROXY, output)
            self.assertIn("identityagent none", output)
            self.assertIn("userknownhostsfile /not-read/pinned-key", output)

    def test_rejects_multi_host_and_includes_without_modification(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profile"
            for text in ("Host *\n", "Host agent-vm\nHost other\n", "Host agent-vm\nInclude=x\n",
                         "Host agent-vm\nMatch\thost x\n"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    client.configure(path)
                self.assertEqual(path.read_text(), text)

    def test_corrupt_holds_fail_closed(self):
        async def check():
            with tempfile.TemporaryDirectory() as temp:
                state = Path(temp) / "holds.json"
                for text in ("[]", '{"bad-token": "job"}', "garbage"):
                    state.write_text(text)
                    with self.assertRaises(ValueError):
                        server.Relay(FakeDomain(), "127.0.0.1", 22, state, os.getuid())
        asyncio.run(check())

    def test_run_keeps_hold_on_failure_releases_on_success(self):
        for result in (0, 7):
            calls = []
            def request(path, message):
                calls.append(message)
                return {"token": "test-hold", "ok": True}
            with mock.patch.object(sys, "argv", ["client", "run", "job", "--", "ssh", "vm"]), \
                    mock.patch.object(client, "request", side_effect=request), \
                    mock.patch.object(client.subprocess, "call", return_value=result) as run:
                self.assertEqual(client.main(), result)
                run.assert_called_once_with(["ssh", "vm"])
            self.assertEqual(calls, [{"op": "hold", "label": "job"}] +
                             ([{"op": "release", "token": "test-hold"}] if result == 0 else []))

    def test_vm_mounts_opt_in_and_fixed_client(self):
        plain = broker.podman_argv("codex", "test", "/git", "/git/project")
        self.assertFalse(any("/opt/agent-vm" in argument for argument in plain))
        enabled = broker.podman_argv("codex", "test", "/git", "/git/project", vm_socket_dir="/run/acp-agent-vm")
        self.assertIn("--mount=type=bind,src=/run/acp-agent-vm,dst=/opt/agent-vm,ro=true", enabled)
        self.assertIn(f"--mount=type=bind,src={broker.VM_CLIENT},dst=/opt/agent-vm-client.py,ro=true", enabled)
        self.assertFalse(any("/run/libvirt" in argument for argument in enabled))


if __name__ == "__main__":
    unittest.main()
