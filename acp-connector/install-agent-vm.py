#!/usr/bin/python3
"""Install the optional system socket/helper for an existing libvirt VM.

Run with sudo on the desktop. This does not edit VM disks, firmware,
networking, SSH authentication or agent-container configuration.
"""

import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def install(source, destination, mode=0o644):
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError(f"refusing symlink: {destination}")
    shutil.copyfile(source, destination)
    destination.chmod(mode)
    os.chown(destination, 0, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="existing qemu:///system domain name")
    parser.add_argument("--address", required=True, type=ipaddress.IPv4Address,
                        help="reserved/static guest IPv4 address")
    parser.add_argument("--socket-user", required=True, help="desktop account running the ACP brokers")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--idle-minutes", type=int, default=15)
    parser.add_argument("--boot-seconds", type=int, default=180)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run this with sudo in a desktop host terminal")
    if not 1 <= args.port <= 65535 or not 1 <= args.idle_minutes <= 1440 or not 1 <= args.boot_seconds <= 1800:
        parser.error("invalid port or timeout")
    user = pwd.getpwnam(args.socket_user)
    if user.pw_uid == 0:
        parser.error("socket-user must be a regular desktop account")
    try:
        import libvirt
    except ImportError:
        parser.error("install the host's libvirt-python bindings matching /usr/bin/python3 (openSUSE packages use python3xx-libvirt-python)")
    with contextlib.closing(libvirt.open("qemu:///system")) as conn:
        domain = conn.lookupByName(args.domain)
        if not domain.isPersistent():
            parser.error("the VM must be a persistent libvirt domain")
        domain_uuid = domain.UUIDString()
        if domain.autostart():
            parser.error("disable this VM's libvirt autostart in Virtual Machine Manager first")

    # Validate the existing configuration before replacing files. Updating an
    # installed helper disconnects relayed SSH; do not do that during work.
    state_file = Path("/var/lib/acp-agent-vm/holds.json")
    if state_file.exists() and json.loads(state_file.read_text()):
        parser.error("keep-awake holds exist; finish/check the jobs and release them before reinstalling")
    active = subprocess.run(["systemctl", "is-active", "--quiet", "acp-agent-vm.service"], check=False)
    if active.returncode == 0:
        parser.error("helper is active; finish agent work, then sudo systemctl stop acp-agent-vm.socket acp-agent-vm.service before reinstalling")

    src = Path(__file__).resolve().parent
    directories = ("/usr/local/libexec/acp-agent-vm", "/etc/systemd/system/acp-agent-vm.socket.d",
                   "/run/acp-agent-vm")
    for name in directories:
        directory = Path(name)
        if directory.is_symlink():
            raise ValueError(f"refusing symlink: {directory}")
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        if directory.stat().st_uid != 0:
            raise ValueError(f"directory must be root-owned: {directory}")
        directory.chmod(0o755)
    install(src / "agent-vm-server.py", "/usr/local/libexec/acp-agent-vm/agent-vm-server.py", 0o555)
    for unit in ("acp-agent-vm.socket", "acp-agent-vm.service"):
        install(src / unit, "/etc/systemd/system/" + unit)
    config = {"domain_uuid": domain_uuid, "address": str(args.address), "port": args.port,
              "socket_uid": user.pw_uid, "idle_seconds": args.idle_minutes * 60,
              "boot_seconds": args.boot_seconds}
    for name, content in (("/etc/acp-agent-vm.json", json.dumps(config, indent=2) + "\n"),
                          ("/etc/systemd/system/acp-agent-vm.socket.d/user.conf",
                           f"[Socket]\nSocketUser={user.pw_uid}\n")):
        destination = Path(name)
        if destination.is_symlink():
            raise ValueError(f"refusing symlink: {destination}")
        destination.write_text(content)
        destination.chmod(0o644)
        os.chown(destination, 0, 0)
    # Avoid user-config dependencies and reconnect safely after socket restart:
    # containers mount the socket's directory, not its current inode.
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "acp-agent-vm.socket"], check=True)
    subprocess.run(["systemctl", "restart", "acp-agent-vm.socket"], check=True)
    print("Installed VM activation. The VM was not started or stopped.")
    print("Next: run ./install.sh --agent-vm as your desktop user.")


if __name__ == "__main__":
    main()
