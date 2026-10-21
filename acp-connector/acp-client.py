#!/usr/bin/python3
"""Zed-side stdio bridge to a host ACP connector socket.

Runs inside the Zed Flatpak as a custom agent server. It only copies bytes
between stdio and the dedicated Unix socket. All ACP policy is enforced by
the host broker, so this file carries no security decisions. It takes no
arguments: Zed must not be able to re-invoke it with auth or command input.
The Zed entry selects the connector with env ACP_CONNECTOR (codex, claude,
codex-teams or claude-teams);
each socket is bound to one fixed container on the host.
"""

import os
import re
import socket
import sys
import threading

NAME = os.environ.get("ACP_CONNECTOR", "")
SOCKET = os.environ.get("ACP_CONNECTOR_SOCKET") or \
    f"/run/user/{os.getuid()}/app/dev.zed.Zed/{NAME}-acp.sock"


def stdin_to_socket(sock, stdin_closed):
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                stdin_closed.set()
                break
            sock.sendall(data)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main():
    if len(sys.argv) > 1:
        print("acp-client: takes no arguments", file=sys.stderr)
        return 2
    if not os.environ.get("ACP_CONNECTOR_SOCKET") and not re.fullmatch(r"[a-z]+(-[a-z]+)*", NAME):
        print("acp-client: set ACP_CONNECTOR (e.g. codex or claude) in the Zed agent entry", file=sys.stderr)
        return 2
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET)
    except OSError as exc:
        print(f"acp-client: cannot connect to {SOCKET}: {exc.strerror}\n"
              f"Start it on the host: systemctl --user enable --now {NAME or '<name>'}-acp-connector.socket",
              file=sys.stderr)
        return 1
    stdin_closed = threading.Event()
    threading.Thread(target=stdin_to_socket, args=(sock, stdin_closed), daemon=True).start()
    try:
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                if stdin_closed.is_set():
                    return 0
                print("acp-client: broker connection closed unexpectedly; "
                      f"see journalctl --user -u {NAME or '<name>'}-acp-connector",
                      file=sys.stderr)
                return 1
            view = memoryview(data)
            while view:
                view = view[os.write(1, view):]
    except (OSError, BrokenPipeError):
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
