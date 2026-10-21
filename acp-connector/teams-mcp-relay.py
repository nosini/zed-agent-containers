#!/usr/bin/python3
"""Stdio MCP server for the Teams agent profiles: teams-mcp-relay.py SOCKET

Runs inside the container, started by the agent for the teams-office MCP
server the broker adds to each session. It only copies bytes between stdio
and Teams for Linux's MCP bridge socket; Teams itself serves MCP. Installed
outside ~/Git and mounted read-only, so the agents can't change it.
"""

import os
import socket
import sys
import threading

CONNECT_TIMEOUT = 5


def stdin_to_socket(sock):
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/"):
        print("usage: teams-mcp-relay.py /absolute/path/to/bridge.sock", file=sys.stderr)
        return 2
    path = sys.argv[1]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(path)
    except OSError as exc:
        print(f"Cannot connect to Teams MCP at {path}: {exc.strerror or exc}. "
              "Start Teams, then start a new thread.", file=sys.stderr)
        return 1
    sock.settimeout(None)
    threading.Thread(target=stdin_to_socket, args=(sock,), daemon=True).start()
    out = sys.stdout.buffer
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            out.write(data)
            out.flush()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
