#!/usr/bin/python3
"""Socket-activated SSH relay and idle shutdown for one fixed libvirt VM.

The root-owned configuration chooses the domain and SSH endpoint. Clients
can only relay SSH, add/remove keep-awake holds, and inspect those holds.
No authentication keys or libvirt sockets are exposed to containers.
"""

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import uuid

LOG = logging.getLogger("agent-vm")
SOCKET_DIR = "/run/acp-agent-vm"
MAX_CONNECTIONS = 64
MAX_CONTROL_CONNECTIONS = 16
MAX_HOLDS = 64
LIBVIRT_URI = "qemu+unix:///system?socket=/run/acp-agent-vm-private/libvirt.sock"


class Domain:
    """Open a fresh connection for each operation; never accept client input."""

    def __init__(self, domain_uuid):
        import libvirt
        self.libvirt = libvirt
        self.uuid = domain_uuid

    def call(self, operation):
        with contextlib.closing(self.libvirt.open(LIBVIRT_URI)) as conn:
            domain = conn.lookupByUUIDString(self.uuid)
            if operation == "state":
                return domain.state()[0], domain.ID()
            if operation == "start":
                domain.create()  # Retain existing firmware NVRAM and TPM state.
                return domain.ID()
            if operation == "shutdown":
                domain.shutdown()  # Graceful only; never destroy/reset the VM.


class Relay:
    def __init__(self, domain, address, port, state_file, allowed_uid,
                 idle_seconds=900, boot_seconds=180, shutdown_seconds=120):
        self.domain = domain
        self.address, self.port = address, port
        self.state_file = Path(state_file)
        self.allowed_uid = allowed_uid
        self.idle_seconds = idle_seconds
        self.boot_seconds = boot_seconds
        self.shutdown_seconds = shutdown_seconds
        self.lock = asyncio.Lock()
        self.active = 0
        self.controls = 0
        self.owned_id = None
        self.holds = self.load_holds()
        self.last_use = asyncio.get_running_loop().time()

    def load_holds(self):
        try:
            holds = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}
        if (not isinstance(holds, dict) or len(holds) > MAX_HOLDS or
                any(not isinstance(k, str) or str(uuid.UUID(k)) != k or
                    not isinstance(v, str) or not 1 <= len(v) <= 128
                    for k, v in holds.items())):
            raise ValueError("invalid hold state; refusing automatic shutdown")
        return holds

    def save_holds(self, holds):
        # Write before acknowledging a hold or release, so a service restart
        # cannot forget a detached build's hold. A corrupt file fails closed.
        temporary = self.state_file.with_suffix(".new")
        with temporary.open("w") as out:
            os.chmod(temporary, 0o600)
            json.dump(holds, out)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(self.state_file)
        fd = os.open(self.state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    async def commit_holds(self, holds):
        # Keep the lifecycle lock until the worker is done, even if a request
        # is cancelled. Other holds must not overwrite an in-flight commit.
        worker = asyncio.create_task(asyncio.to_thread(self.save_holds, holds))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.cancelled():  # Whole-process asyncio shutdown.
                    raise
                cancelled = True
        worker.result()
        self.holds = holds
        self.touch()
        if cancelled:
            raise asyncio.CancelledError

    def touch(self):
        self.last_use = asyncio.get_running_loop().time()

    def permitted(self, writer):
        sock = writer.get_extra_info("socket")
        _, uid, _ = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == self.allowed_uid

    async def state(self):
        state, runtime_id = await asyncio.to_thread(self.domain.call, "state")
        if self.owned_id is not None and runtime_id != self.owned_id:
            self.owned_id = None
        if self.owned_id is not None and state in (3, 7):  # PAUSED, PMSUSPENDED
            self.owned_id = None
            LOG.info("VM paused/suspended; leaving it under manual control")
        return state

    async def ensure_running(self):
        async with self.lock:
            state = await self.state()
            if state == 5:  # VIR_DOMAIN_SHUTOFF
                LOG.info("starting configured VM")
                self.owned_id = await asyncio.to_thread(self.domain.call, "start")
            elif state not in (1, 2):  # RUNNING, BLOCKED
                raise RuntimeError("VM is paused, shutting down or in an unexpected state")

    async def backend(self, identification):
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.boot_seconds
        waiting = False

        async def connect():
            reader, writer = await asyncio.open_connection(self.address, self.port, limit=4096)
            try:
                # Replay only the client's public identification on retries.
                # Key exchange and authentication remain with the SSH client.
                writer.write(identification)
                await writer.drain()
                banner = bytearray()
                while len(banner) < 8192:
                    line = await reader.readline()
                    if not line.endswith(b"\n"):
                        raise ConnectionError("VM closed before its SSH identification")
                    banner.extend(line)
                    if line.startswith(b"SSH-"):
                        if len(line) > 255 or b"\0" in line:
                            raise ConnectionError("invalid VM SSH identification")
                        return reader, writer, bytes(banner)
                raise ConnectionError("VM SSH preamble is too large")
            except BaseException:
                writer.close()
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 0.5)
                raise

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("VM SSH did not become ready before the boot timeout")
            try:
                connection = await asyncio.wait_for(connect(), min(10, remaining))
                if waiting:
                    LOG.info("VM SSH became ready after %.1f seconds", loop.time() - started)
                return connection
            except (OSError, ValueError, asyncio.TimeoutError):
                if not waiting:
                    LOG.info("waiting for VM SSH readiness")
                    waiting = True
                await asyncio.sleep(min(0.5, max(0, deadline - loop.time())))

    @staticmethod
    async def copy(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
            await writer.drain()

    async def ssh(self, reader, writer):
        if not self.permitted(writer) or self.active >= MAX_CONNECTIONS:
            writer.close()
            return
        self.active += 1  # Waiting for a cold boot also keeps the VM awake.
        self.touch()
        remote = None
        tasks = []
        try:
            await self.ensure_running()
            identification = await asyncio.wait_for(reader.readline(), self.boot_seconds)
            if (not identification.startswith(b"SSH-") or not identification.endswith(b"\n") or
                    len(identification) > 255 or b"\0" in identification):
                raise RuntimeError("invalid SSH client identification")
            remote_reader, remote, banner = await self.backend(identification)
            writer.write(banner)
            await writer.drain()
            tasks = [asyncio.create_task(self.copy(reader, remote)),
                     asyncio.create_task(self.copy(remote_reader, writer))]
            await asyncio.gather(*tasks)
        except (OSError, RuntimeError, TimeoutError) as exc:
            LOG.error("SSH relay failed: %s", exc)
        except Exception:
            LOG.exception("SSH relay failed")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (remote, writer):
                if stream:
                    stream.close()
                    with contextlib.suppress(OSError):
                        await stream.wait_closed()
            self.active -= 1
            self.touch()

    async def control(self, reader, writer):
        if not self.permitted(writer):
            writer.close()
            return
        if self.controls >= MAX_CONTROL_CONNECTIONS:
            # Consume a normal request before closing so unread input does not
            # reset the socket and hide the busy reply. Bound this extra wait.
            with contextlib.suppress(OSError, ValueError, asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), 0.1)
            await self.control_reply(writer, {"ok": False, "error": "VM control busy; retry shortly"})
            return
        self.controls += 1
        try:
            try:
                reply = await self.control_request(reader)
                result = {"ok": True, **reply}
            except (ValueError, TypeError, asyncio.TimeoutError) as exc:
                result = {"ok": False, "error": str(exc)}
            except Exception:
                LOG.exception("keep-awake request failed")
                result = {"ok": False, "error": "cannot update holds; check the host service journal"}
            await self.control_reply(writer, result)
        finally:
            writer.close()
            self.controls -= 1

    async def control_request(self, reader):
        line = await asyncio.wait_for(reader.readline(), 5)
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = request.get("op")
        if operation == "status" and set(request) == {"op"}:
            # This snapshot requires neither disk access nor the lifecycle
            # lock, so it remains available during shutdown or a slow commit.
            return {"holds": self.holds, "ssh_connections": self.active,
                    "managed": self.owned_id is not None,
                    "boot_seconds": self.boot_seconds, "shutdown_seconds": self.shutdown_seconds}
        async with self.lock:
            if operation == "hold" and set(request) == {"op", "label"}:
                label = request["label"]
                if not isinstance(label, str) or not 1 <= len(label) <= 128:
                    raise ValueError("hold label must contain 1–128 characters")
                if len(self.holds) >= MAX_HOLDS:
                    raise ValueError("too many holds; release completed work first")
                token = str(uuid.uuid4())
                await self.commit_holds({**self.holds, token: label})
                reply = {"token": token}
            elif operation == "release" and set(request) == {"op", "token"}:
                token = request["token"]
                if not isinstance(token, str) or token not in self.holds:
                    raise ValueError("unknown hold token")
                await self.commit_holds({k: v for k, v in self.holds.items() if k != token})
                reply = {"released": token}
            else:
                raise ValueError("unknown operation or fields")
            return reply

    @staticmethod
    async def control_reply(writer, result):
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def idle_check(self):
        if (self.active or self.holds or self.owned_id is None or
                asyncio.get_running_loop().time() - self.last_use < self.idle_seconds):
            return
        async with self.lock:
            state = await self.state()
            if self.active or self.holds or self.owned_id is None or state not in (1, 2):
                return
            LOG.info("idle timeout reached; requesting graceful VM shutdown")
            # Relinquish ownership before attempting shutdown. A failed API
            # call or subsequent status read must not trigger another attempt.
            self.owned_id = None
            await asyncio.to_thread(self.domain.call, "shutdown")
            deadline = asyncio.get_running_loop().time() + self.shutdown_seconds
            while await self.state() != 5:
                if asyncio.get_running_loop().time() >= deadline:
                    # A refusal must never become a forced power-off or repeated
                    # shutdown attempts. Leave the VM under manual control.
                    self.owned_id = None
                    LOG.error("VM did not shut down; leaving it running for inspection")
                    break
                await asyncio.sleep(0.5)
            self.touch()

    async def idle_loop(self):
        while True:
            await asyncio.sleep(min(5, self.idle_seconds))
            try:
                await self.idle_check()
            except Exception:
                LOG.exception("idle check failed; leaving VM running")
                self.touch()


def load_config(path):
    info = os.stat(path)
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("configuration must be root-owned and not group/world writable")
    config = json.loads(Path(path).read_text())
    expected = {"domain_uuid", "address", "port", "socket_uid", "idle_seconds", "boot_seconds"}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("unexpected configuration fields")
    config["domain_uuid"] = str(uuid.UUID(config["domain_uuid"]))
    ipaddress.IPv4Address(config["address"])
    for key, low, high in (("port", 1, 65535), ("socket_uid", 1, 2**32-2),
                           ("idle_seconds", 1, 86400), ("boot_seconds", 1, 1800)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"invalid {key}")
    return config


def activated_sockets():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "2":
        raise RuntimeError("start this helper through acp-agent-vm.socket")
    sockets = {}
    for fd in (3, 4):
        sock = socket.socket(fileno=fd)
        name = sock.getsockname()
        if (sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM or
                not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) or
                name not in (SOCKET_DIR + "/ssh.sock", SOCKET_DIR + "/control.sock") or
                name in sockets):
            raise RuntimeError("unexpected activation socket")
        sock.setblocking(False)
        sockets[name] = sock
    return sockets


async def serve(config, sockets):
    relay = Relay(Domain(config["domain_uuid"]), config["address"], config["port"],
                  "/var/lib/acp-agent-vm/holds.json", config["socket_uid"],
                  config["idle_seconds"], config["boot_seconds"])
    servers = []
    try:
        for name, sock in sockets.items():
            callback = relay.ssh if name.endswith("/ssh.sock") else relay.control
            servers.append(await asyncio.start_unix_server(callback, sock=sock, limit=4096))
        await relay.idle_loop()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        # Stopping/restarting this host service never powers off the guest.


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="/etc/acp-agent-vm.json")
    args = parser.parse_args()
    if os.geteuid() == 0 or os.geteuid() != pwd.getpwnam("acp-agent-vm").pw_uid:
        parser.error("run through the service as the dedicated acp-agent-vm account")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(serve(load_config(args.config), activated_sockets()))


if __name__ == "__main__":
    main()
