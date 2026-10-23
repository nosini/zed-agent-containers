#!/usr/bin/python3
"""Stdio MCP server for the Teams agent profiles: teams-mcp-relay.py SOCKET

Runs inside the container, started by the agent for the teams-office MCP
server the broker adds to each session. It only copies bytes between stdio
and the broker's socket, which passes them on to Teams for Linux's MCP
bridge; Teams itself serves MCP. Installed outside ~/Git and mounted
read-only, so the agents can't change it.
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
        # The broker's socket: gone only if the broker stopped.
        print(f"Cannot connect to the Teams MCP forwarder at {path}: {exc.strerror or exc}. "
              "Reconnect the agent in Zed.", file=sys.stderr)
        return 1
    sock.settimeout(None)
    threading.Thread(target=stdin_to_socket, args=(sock,), daemon=True).start()
    out = sys.stdout.buffer
    replied = False
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            replied = True
            out.write(data)
            out.flush()
    except OSError:
        pass
    if not replied:
        # Each thread starts its own relay, and the broker looks up Teams'
        # socket for every connection, so a new thread sees a restarted Teams.
        print("Teams MCP closed the connection without replying. Start Teams if it isn't running "
              "(it accepts four MCP connections at a time), then start a new thread.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
