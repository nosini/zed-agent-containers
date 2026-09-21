"""Test harness: run the broker with a substitute agent command.

  run_broker.py mock SOCKET GIT_ROOT [MAX_LINE]    -- hostile mock agent
  run_broker.py real SOCKET GIT_ROOT CMD [ARGS...] -- a real ACP adapter
  run_broker.py activate SOCKET GIT_ROOT PROFILE   -- production main() via socket activation
"""

import asyncio
import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER = os.path.join(HERE, "..", "acp-broker.py")


def load_broker():
    spec = importlib.util.spec_from_file_location("broker", BROKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mode, sock_path, git_root = sys.argv[1:4]
    if mode == "activate":
        # Emulate systemd: listening socket on fd 3, then exec the real entry point.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(sock_path)
        sock.listen(4)
        if sock.fileno() != 3:
            os.dup2(sock.fileno(), 3)
        os.set_inheritable(3, True)
        os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS="1")
        os.execv(sys.executable, [sys.executable, "-I", BROKER, "--profile", sys.argv[4], "--git-root", git_root,
                                  *sys.argv[5:]])
    broker = load_broker()
    if mode == "mock":
        if len(sys.argv) > 4:
            broker.MAX_LINE = int(sys.argv[4])
        base = [sys.executable, os.path.join(HERE, "mock_agent.py")]
        launcher = lambda project, name: (base + [project], None)  # mock logs the project it got
    else:
        launcher = lambda project, name: (sys.argv[4:], None)
    asyncio.run(broker.serve(broker.bind_socket(sock_path), launcher, git_root))


if __name__ == "__main__":
    main()
