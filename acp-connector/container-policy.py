#!/usr/bin/python3
"""Shared restrictions for agent containers and VM-profile setup containers."""

import argparse
import os
import subprocess
import sys

PODMAN = ("/usr/bin/podman", "--storage-opt", "overlay.mount_program=/usr/bin/fuse-overlayfs")
RUN_FLAGS = ("--pull=never", "--log-driver=none", "--userns=keep-id:uid=1000,gid=1000",
             "--user=1000:1000", "--read-only", "--read-only-tmpfs=true", "--cap-drop=all",
             "--security-opt=no-new-privileges", "--security-opt=label=disable", "--pids-limit=4096")


def run_argv(interactive=False):
    return [*PODMAN, "run", "--rm", *(["--interactive"] if interactive else []), *RUN_FLAGS]


def configure_vm_argv(client, sockets):
    for path in (client, sockets):
        if not os.path.isabs(path) or os.path.normpath(path) != path or "," in path or "=" in path:
            raise ValueError("mount paths must be absolute and normalized, without ',' or '='")
    return [*run_argv(), "--network=none", "--mount=type=volume,src=agent-home,dst=/home/node",
            f"--mount=type=bind,src={client},dst=/opt/agent-vm-client.py,ro=true",
            f"--mount=type=bind,src={sockets},dst=/opt/agent-vm,ro=true",
            "--entrypoint=/usr/bin/python3", "localhost/codex-agent:local",
            "-I", "/opt/agent-vm-client.py", "configure"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["configure-vm"])
    parser.add_argument("--client", required=True)
    parser.add_argument("--sockets", required=True)
    args = parser.parse_args()
    return subprocess.call(configure_vm_argv(args.client, args.sockets))


if __name__ == "__main__":
    sys.exit(main())
