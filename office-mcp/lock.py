#!/usr/bin/env python3
"""Pin the Word and Excel MCP servers and their dependencies with hashes.

    python3 -I lock.py [--cutoff ISO-TIME] [docx-mcp-server==X] [excel-mcp-server==Y]

Writes docx.txt and excel.txt next to this script, which the agent images
install with pip --require-hashes --no-deps, and pip.txt, the pinned pip
this script resolves with. A server without a version on the command line
keeps its current pin. Dependencies are the newest releases uploaded
before --cutoff (none: the newest). Run it with the agent images' Python
(update.sh does), since the pins are for that interpreter and platform.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HERE = pathlib.Path(__file__).resolve().parent

# The packages, the requirements added to theirs, and dependencies left out.
SERVERS = {
    "docx": {
        "package": "docx-mcp-server",
        "extras": ["equations"],
        # docx-mcp uses the mcp 1.x FastMCP API, which mcp 2 removed.
        "constraints": ["mcp<2"],
        # Only scrub_pii uses these: hundreds of MB, and a 560 MB spaCy model
        # it would download at runtime into the read-only image.
        "exclude": ["presidio-analyzer", "presidio-anonymizer", "spacy"],
    },
    "excel": {
        "package": "excel-mcp-server",
        "extras": [],
        "constraints": [],
        "exclude": [],
    },
}
PIN = re.compile(r"^([A-Za-z0-9._-]+)==(\S+)")


def pinned(name):
    """The version of the server package in its current lock file."""
    path = HERE / f"{name}.txt"
    if path.exists():
        package = canonicalize_name(SERVERS[name]["package"])
        for line in path.read_text().splitlines():
            m = PIN.match(line)
            if m and canonicalize_name(m.group(1)) == package:
                return m.group(2)
    return None


def resolve(pip, requirements, cutoff, no_deps=False):
    """pip's resolution as {name: (version, sha256, metadata)}."""
    argv = [pip, "install", "--dry-run", "--ignore-installed", "--only-binary=:all:",
            "--no-cache-dir", "--quiet", "--report=-", "--disable-pip-version-check"]
    if cutoff:
        argv.append(f"--uploaded-prior-to={cutoff}")
    if no_deps:
        argv.append("--no-deps")
    report = json.loads(subprocess.run(argv + ["--"] + requirements, check=True,
                                       capture_output=True, text=True).stdout)
    out = {}
    for item in report["install"]:
        meta = item["metadata"]
        hashes = item["download_info"].get("archive_info", {}).get("hashes", {})
        if "sha256" not in hashes:
            sys.exit(f"lock.py: no sha256 for {meta['name']} {meta['version']}")
        out[canonicalize_name(meta["name"])] = (meta["version"], hashes["sha256"], meta)
    return out


def lock_server(pip, name, version, cutoff):
    spec = SERVERS[name]
    package = canonicalize_name(spec["package"])
    extras = f"[{','.join(spec['extras'])}]" if spec["extras"] else ""
    top = resolve(pip, [f"{package}{extras}=={version}"], None, no_deps=True)[package]
    # The package's own requirements, with its extras, minus the exclusions.
    deps = []
    for line in top[2].get("requires_dist", []):
        req = Requirement(line)
        envs = [{"extra": e} for e in spec["extras"]] or [{"extra": ""}]
        if req.marker and not any(req.marker.evaluate(env) for env in envs):
            continue
        if canonicalize_name(req.name) in map(canonicalize_name, spec["exclude"]):
            continue
        req.marker = None
        deps.append(str(req))
    resolved = resolve(pip, deps + spec["constraints"], cutoff)
    resolved[package] = top
    header = [f"# {spec['package']} {version} for the agent images, written by lock.py."]
    if spec["exclude"]:
        header.append(f"# Left out: {', '.join(spec['exclude'])}.")
    write(HERE / f"{name}.txt", header, resolved)


def write(path, header, resolved):
    lines = header + ["# Install with pip install --no-deps --require-hashes -r FILE."]
    for name in sorted(resolved):
        version, sha256, _ = resolved[name]
        lines.append(f"{name}=={version} --hash=sha256:{sha256}")
    path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cutoff", help="only dependencies uploaded before this ISO time")
    ap.add_argument("pins", nargs="*", metavar="PACKAGE==VERSION")
    args = ap.parse_args()
    versions = {name: pinned(name) for name in SERVERS}
    by_package = {canonicalize_name(s["package"]): n for n, s in SERVERS.items()}
    for pin in args.pins:
        m = PIN.match(pin)
        if not m or canonicalize_name(m.group(1)) not in by_package:
            ap.error(f"expected docx-mcp-server==X or excel-mcp-server==Y, not {pin!r}")
        versions[by_package[canonicalize_name(m.group(1))]] = m.group(2)
    for name, version in versions.items():
        if version is None:
            ap.error(f"{SERVERS[name]['package']} has no pin yet; pass its version")

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "venv", tmp], check=True)
        pip = f"{tmp}/bin/pip"
        # Resolve with the pinned pip: Debian's lacks --uploaded-prior-to.
        subprocess.run([pip, "install", "--quiet", "--disable-pip-version-check",
                        "--no-deps", "--require-hashes", "-r", HERE / "pip.txt"], check=True)
        for name, version in versions.items():
            lock_server(pip, name, version, args.cutoff)
        write(HERE / "pip.txt", ["# pip for lock.py, written by lock.py."],
              resolve(pip, ["pip"], args.cutoff, no_deps=True))


if __name__ == "__main__":
    main()
