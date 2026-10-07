#!/usr/bin/python3
"""Read-only/idempotent installation checks under the dedicated helper UID."""

import argparse
import contextlib
import os
import pwd


def check(conn, libvirt, domain_uuid, other_uuid=None):
    domain = conn.lookupByUUIDString(domain_uuid)
    domain.state()

    def denied(label, operation, hidden=False):
        try:
            operation()
        except libvirt.libvirtError as exc:
            codes = {libvirt.VIR_ERR_ACCESS_DENIED, libvirt.VIR_ERR_AUTH_FAILED,
                     libvirt.VIR_ERR_OPERATION_DENIED}
            if hidden:
                codes.add(libvirt.VIR_ERR_NO_DOMAIN)
            if exc.get_error_code() in codes:
                return
            raise RuntimeError(f"{label}: unexpected libvirt error") from exc
        raise RuntimeError(f"{label} was allowed; refusing to enable the helper")

    denied("listing all VMs", lambda: conn.listAllDomains(0))
    denied("reading secure VM XML", lambda: domain.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE))
    # The installer requires autostart already off, so an unexpected success
    # leaves the VM unchanged while proving that write access was granted.
    denied("changing VM autostart", lambda: domain.setAutostart(0))
    if other_uuid:
        denied("accessing another VM", lambda: conn.lookupByUUIDString(other_uuid), hidden=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain_uuid")
    parser.add_argument("other_uuid", nargs="?")
    args = parser.parse_args()
    if os.geteuid() == 0 or os.geteuid() != pwd.getpwnam("acp-agent-vm").pw_uid:
        parser.error("run as the dedicated acp-agent-vm account")
    import libvirt
    libvirt.registerErrorHandler(lambda *_: None, None)
    uri = "qemu+unix:///system?socket=/run/libvirt/virtqemud-sock"
    with contextlib.closing(libvirt.open(uri)) as conn:
        check(conn, libvirt, args.domain_uuid, args.other_uuid)
    print("VM state access works; broader libvirt operations are denied.")


if __name__ == "__main__":
    main()
