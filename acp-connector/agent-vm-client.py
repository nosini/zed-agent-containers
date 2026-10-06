#!/usr/bin/python3
"""Container-side transport and keep-awake controls for the build/test VM."""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import socket
import subprocess
import sys
import tempfile

SOCKET_DIR = "/opt/agent-vm"
PROXY = "python3 -I /opt/agent-vm-client.py proxy"
MAX_REPLY = 262144  # Includes 64 maximally escaped Unicode hold labels.


def proxy(path):
    """Copy SSH bytes with backpressure and preserve stdin's half-close."""
    incoming = bytearray()
    outgoing = bytearray()
    stdin_open, remote_open = True, True
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    limit = 262144
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
        sock.setblocking(False)
        os.set_blocking(stdin, False)
        os.set_blocking(stdout, False)
        with selectors.DefaultSelector() as selector:
            while remote_open or incoming:
                # Bounded queues; never block one direction while the other
                # needs draining (large simultaneous stdin/stdout transfers).
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fileobj)
                if remote_open and stdin_open and len(outgoing) < limit:
                    selector.register(stdin, selectors.EVENT_READ)
                if incoming:
                    selector.register(stdout, selectors.EVENT_WRITE)
                events = (selectors.EVENT_READ if remote_open and len(incoming) < limit else 0)
                if remote_open and outgoing:
                    events |= selectors.EVENT_WRITE
                if events:
                    selector.register(sock, events)
                if remote_open and stdin_open is False and not outgoing:
                    sock.shutdown(socket.SHUT_WR)
                    # Only half-close once, keeping stdout alive until EOF.
                    stdin_open = None
                for key, _ in selector.select():
                    if key.fileobj == stdin:
                        data = os.read(stdin, 65536)
                        if data:
                            outgoing.extend(data)
                        else:
                            stdin_open = False
                    elif key.fileobj == stdout:
                        del incoming[:os.write(stdout, incoming)]
                    else:
                        if _ & selectors.EVENT_WRITE:
                            del outgoing[:sock.send(outgoing)]
                        if _ & selectors.EVENT_READ:
                            data = sock.recv(65536)
                            if data:
                                incoming.extend(data)
                            else:
                                remote_open = False


def request(path, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        # Includes time to finish a shutdown already underway. Hold acquisition
        # does not return until the server has committed the hold to disk.
        sock.settimeout(180)
        sock.connect(path)
        sock.sendall(json.dumps(message).encode() + b"\n")
        with sock.makefile("rb") as stream:
            line = stream.readline(MAX_REPLY + 1)
        if not line.endswith(b"\n") or len(line) > MAX_REPLY:
            raise ValueError("invalid VM control reply")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise ValueError("invalid VM control reply")
        if result.get("ok") is not True:
            raise ValueError(result.get("error", "VM control request failed"))
        return result


def configure(path, connect_timeout=1980):
    """Add only the activation transport to a dedicated, single-host profile."""
    text = path.read_text()
    lines = text.splitlines()
    def option(line):
        match = re.match(r"^\s*([A-Za-z]+)(?:\s+|=)(.*)$", line)
        return (match[1].lower(), match[2]) if match else (None, None)
    hosts = [shlex.split(option(line)[1], comments=True) for line in lines if option(line)[0] == "host"]
    if (hosts != [["agent-vm"]] or any(option(line)[0] in ("match", "include") for line in lines)):
        raise ValueError("expected a dedicated profile with just Host agent-vm and no Match/Include")
    lines = [line for line in lines if option(line)[0] not in ("proxycommand", "proxyjump", "connecttimeout")]
    lines.extend(["    ProxyCommand " + PROXY, f"    ConnectTimeout {connect_timeout}"])
    replacement = "\n".join(lines) + "\n"
    if replacement != text:
        # Never read the private key or change host-key/authentication settings.
        fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as out:
                out.write(replacement)
                out.flush()
                os.fsync(out.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket-dir", default=SOCKET_DIR,
                        help="activation sockets (on the desktop: /run/acp-agent-vm)")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("proxy", help="SSH ProxyCommand; do not invoke interactively")
    sub.add_parser("status", help="list holds and relay activity")
    hold = sub.add_parser("hold", help="keep awake until this token is released")
    hold.add_argument("label")
    release = sub.add_parser("release")
    release.add_argument("token")
    run = sub.add_parser("run", help="hold the VM while a local command runs")
    run.add_argument("label")
    run.add_argument("command", nargs=argparse.REMAINDER)
    setup = sub.add_parser("configure", help="enable activation in the existing VM SSH profile")
    setup.add_argument("--profile", type=Path, default=Path.home() / ".ssh/agent-vm.conf")
    args = parser.parse_args()
    path = args.socket_dir + "/control.sock"
    try:
        if args.operation == "proxy":
            proxy(args.socket_dir + "/ssh.sock")
        elif args.operation == "configure":
            if not Path(args.socket_dir + "/ssh.sock").is_socket():
                raise ValueError("VM activation socket is not mounted; reconnect after host installation")
            status = request(path, {"op": "status"})
            # Older helpers do not report timings; cover the longest supported
            # boot in that case, plus shutdown and a startup margin.
            boot, shutdown = status.get("boot_seconds", 1800), status.get("shutdown_seconds", 120)
            if (type(boot) is not int or not 1 <= boot <= 1800 or
                    type(shutdown) is not int or not 1 <= shutdown <= 120):
                raise ValueError("invalid VM helper timeout settings")
            configure(args.profile, connect_timeout=boot + shutdown + 60)
            print(f"Enabled VM activation in {args.profile}")
        elif args.operation == "status":
            print(json.dumps(request(path, {"op": "status"}), indent=2))
        elif args.operation == "release":
            request(path, {"op": "release", "token": args.token})
        elif args.operation == "hold":
            print(request(path, {"op": "hold", "label": args.label})["token"])
        else:
            command = args.command
            if command[:1] == ["--"]:
                command = command[1:]
            if not command:
                raise ValueError("run requires a command")
            token = request(path, {"op": "hold", "label": args.label})["token"]
            # On interruption or abnormal child failure, retain the hold: the
            # remote job may still be running. status/release recover it later.
            print(f"VM hold: {token}", file=sys.stderr)
            code = subprocess.call(command)
            if code == 0:
                request(path, {"op": "release", "token": token})
            else:
                print("Command failed; hold retained. Verify remote work before releasing it.", file=sys.stderr)
            return code if code >= 0 else 128 - code
    except (OSError, ValueError) as exc:
        print(f"agent-vm: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("agent-vm: interrupted; any job hold was retained", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
