#!/usr/bin/python3
"""Install the optional system socket/helper for an existing libvirt VM.

Run with sudo on the desktop. This does not edit VM disks, firmware,
networking, SSH authentication or agent-container configuration.
"""

import argparse
import ast
import contextlib
import grp
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

ACCOUNT = "acp-agent-vm"
QEMU_CONFIG = Path("/etc/libvirt/virtqemud.conf")
ACL_RULE = Path("/etc/polkit-1/rules.d/00-acp-agent-vm.rules")
ACL_STATE = Path("/etc/acp-agent-vm-acl.json")


def ensure_directory(path):
    path = Path(path)
    for directory in reversed([path, *path.parents]):
        if directory.is_symlink():
            raise ValueError(f"refusing symlink directory: {directory}")
        directory.mkdir(mode=0o755, exist_ok=True)
        info = directory.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"directory must be root-owned and not writable by others: {directory}")


def read_owned(path):
    """Refuse writable/symlink configuration before root reads or replaces it."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"file must be regular, root-owned and not writable by others: {path}")
    return path.read_text()


def write_owned(path, text, mode=0o644):
    path = Path(path)
    read_owned(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), mode)
            os.fchown(out.fileno(), 0, 0)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def acl_config(text):
    """Enable polkit without interpreting/replacing unrelated libvirt options."""
    entries = list(re.finditer(r"(?m)^\s*access_drivers\s*=", text))
    if len(entries) > 1:
        raise ValueError("duplicate access_drivers settings; resolve these before installation")
    if not entries:
        return text.rstrip() + '\n\n# Agent VM: enforce per-operation authorization.\naccess_drivers = ["polkit"]\n', True
    match = re.match(r"\s*(\[[\s\S]*?\])", text[entries[0].end():])
    if not match:
        raise ValueError("cannot parse access_drivers; configure polkit ACL manually")
    drivers = ast.literal_eval(match[1])
    if drivers == ["polkit"]:
        return text, False
    if drivers not in ([], ["none"]):
        raise ValueError("custom access_drivers found; configure polkit ACL manually")
    first = entries[0].end() + match.start(1)
    last = entries[0].end() + match.end(1)
    return text[:first] + '["polkit"]' + text[last:], True


def policy(src, domain_uuid, legacy_access):
    domain_uuid = str(uuid.UUID(domain_uuid))
    template = (src / "acp-agent-vm.rules.in").read_text()
    return template.replace("@DOMAIN_UUID@", json.dumps(domain_uuid)).replace(
        "@LEGACY_ACCESS@", "true" if legacy_access else "false")


def configure_acl(src, domain_uuid, other_uuid=None):
    original = read_owned(QEMU_CONFIG)
    # Some distributions ship the default in /usr/etc. An /etc copy must
    # retain those options instead of replacing them with an empty config.
    base = original
    if base is None:
        base = read_owned(Path("/usr/etc/libvirt/virtqemud.conf")) or ""
    configured, changed = acl_config(base)
    old_rule = read_owned(ACL_RULE)
    if old_rule is not None and not old_rule.startswith("// Managed by install-agent-vm.py."):
        raise ValueError(f"refusing to overwrite an unmanaged rule: {ACL_RULE}")
    state = read_owned(ACL_STATE)
    previous = json.loads(state) if state else {}
    if previous and (set(previous) != {"legacy_qemu_access"} or
                     type(previous["legacy_qemu_access"]) is not bool):
        raise ValueError("invalid previous ACL migration state")
    legacy = changed or previous.get("legacy_qemu_access", False)
    backup = QEMU_CONFIG.with_name(QEMU_CONFIG.name + ".acp-agent-vm-backup")
    if changed and read_owned(backup) is None:
        write_owned(backup, base, 0o600)
    try:
        write_owned(ACL_RULE, policy(src, domain_uuid, legacy))
        if changed:
            write_owned(QEMU_CONFIG, configured)
            subprocess.run(["systemctl", "restart", "virtqemud.service"], check=True)
        # Polkit notices new rules asynchronously. Prove ACL enforcement on
        # the live daemon; a config edit alone is not sufficient evidence.
        command = [shutil.which("runuser") or "/usr/sbin/runuser", "-u", ACCOUNT, "--",
                   "/usr/bin/python3", "-I", "/usr/local/libexec/acp-agent-vm/agent-vm-check-acl.py", domain_uuid]
        if other_uuid:
            command.append(other_uuid)
        for attempt in range(10):
            checked = subprocess.run(command, capture_output=True, text=True, timeout=10)
            if checked.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("libvirt ACL verification failed:\n" + checked.stderr.strip())
        write_owned(ACL_STATE, json.dumps({"legacy_qemu_access": legacy}) + "\n")
        print(checked.stdout.strip())
    except BaseException:
        # Remove the new connection grant before restoring a daemon that may
        # have no ACL. Never enable an unverified unprivileged helper.
        for path, content in ((ACL_RULE, old_rule), (QEMU_CONFIG, original)):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write_owned(path, content)
        if changed:
            subprocess.run(["systemctl", "try-restart", "virtqemud.service"], check=False)
        raise


def install(source, destination, mode=0o644):
    write_owned(destination, Path(source).read_text(), mode)


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
    if (not Path("/run/libvirt/virtqemud-sock").is_socket() or
            subprocess.run(["systemctl", "is-active", "--quiet", "virtqemud.socket"], check=False).returncode != 0):
        parser.error("this installation requires an active modular virtqemud Unix socket")
    with contextlib.closing(libvirt.open("qemu+unix:///system?socket=/run/libvirt/virtqemud-sock")) as conn:
        domain = conn.lookupByName(args.domain)
        if not domain.isPersistent():
            parser.error("the VM must be a persistent libvirt domain")
        domain_uuid = domain.UUIDString()
        if domain.autostart():
            parser.error("disable this VM's libvirt autostart in Virtual Machine Manager first")
        other_uuid = next((item.UUIDString() for item in conn.listAllDomains(0)
                           if item.UUIDString() != domain_uuid), None)

    # Validate the existing configuration before replacing files. Updating an
    # installed helper disconnects relayed SSH; do not do that during work.
    state_file = Path("/var/lib/acp-agent-vm/holds.json")
    if state_file.exists() and json.loads(state_file.read_text()):
        parser.error("keep-awake holds exist; finish/check the jobs and release them before reinstalling")
    for unit in ("acp-agent-vm.socket", "acp-agent-vm.service"):
        if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0:
            parser.error("finish agent work, then sudo systemctl stop acp-agent-vm.socket acp-agent-vm.service before reinstalling")
    if subprocess.run(["systemctl", "is-active", "--quiet", "polkit.service"], check=False).returncode != 0:
        parser.error("polkit.service must be running for libvirt ACL enforcement")
    # Validate config syntax before creating the account or replacing files.
    acl_config(read_owned(QEMU_CONFIG) or read_owned("/usr/etc/libvirt/virtqemud.conf") or "")

    src = Path(__file__).resolve().parent
    directories = ("/usr/local/libexec/acp-agent-vm", "/etc/systemd/system/acp-agent-vm.socket.d",
                   "/etc/systemd/system/acp-agent-vm.service.d", "/etc/sysusers.d",
                   "/etc/polkit-1/rules.d", "/etc/libvirt", "/run/acp-agent-vm")
    for name in directories:
        ensure_directory(name)
    sysusers = Path("/etc/sysusers.d/acp-agent-vm.conf")
    sysusers_text = (src / "acp-agent-vm.sysusers").read_text()
    try:
        existing = pwd.getpwnam(ACCOUNT)
    except KeyError:
        existing = None
    if existing and read_owned(sysusers) != sysusers_text:
        parser.error("acp-agent-vm account already exists without this installer's sysusers definition")
    write_owned(sysusers, sysusers_text)
    subprocess.run(["systemd-sysusers", str(sysusers)], check=True)
    helper = pwd.getpwnam(ACCOUNT)
    helper_group = grp.getgrnam(ACCOUNT)
    if (helper.pw_uid in (0, user.pw_uid) or helper.pw_gid == 0 or
            helper.pw_gid != helper_group.gr_gid or helper.pw_dir != "/nonexistent" or
            not helper.pw_shell.endswith("/nologin") or
            set(os.getgrouplist(ACCOUNT, helper.pw_gid)) != {helper.pw_gid}):
        parser.error("helper account must be dedicated, without login or supplementary groups")
    install(src / "agent-vm-check-acl.py", "/usr/local/libexec/acp-agent-vm/agent-vm-check-acl.py", 0o555)
    configure_acl(src, domain_uuid, other_uuid)
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
        write_owned(destination, content)
    write_owned("/etc/systemd/system/acp-agent-vm.service.d/network.conf",
                f"[Service]\nIPAddressDeny=any\nIPAddressAllow={args.address}/32\n")
    # Avoid user-config dependencies and reconnect safely after socket restart:
    # containers mount the socket's directory, not its current inode.
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "acp-agent-vm.socket"], check=True)
    subprocess.run(["systemctl", "restart", "acp-agent-vm.socket"], check=True)
    print("Installed VM activation as the unprivileged acp-agent-vm account.")
    print("The configured VM was not started or stopped. For first-time connector setup: ./install.sh --agent-vm")


if __name__ == "__main__":
    main()
