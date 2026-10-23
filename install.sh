#!/bin/sh
# Full setup: build both agent images, install the connectors, enable the
# socket units and run the smoke tests. Run from a HOST terminal (not in Zed).
#   ./install.sh               everything
#   ./install.sh --no-build    skip the image builds (after editing only the broker)
#   ./install.sh --no-test     skip the smoke tests
#   ./install.sh --agent-vm   enable the installed VM helper
# Safe to re-run; acp-connector/install.sh alone only reinstalls the host files.
set -eu

build=1 test=1 agent_vm=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --no-build) build=0 ;;
        --no-test) test=0 ;;
        --agent-vm) agent_vm=1 ;;
        --vm-socket-dir)
            if [ "$#" -lt 2 ] || [ "$2" != /run/acp-agent-vm ]; then
                echo "usage: $0 [--no-build] [--no-test] [--agent-vm]" >&2; exit 2
            fi
            agent_vm=1  # Compatibility with the original setup command.
            shift ;;
        *) echo "usage: $0 [--no-build] [--no-test] [--agent-vm]" >&2; exit 2 ;;
    esac
    shift
done

if [ "$agent_vm" = 1 ] && [ ! -d /run/acp-agent-vm ]; then
    echo "Install the VM helper first; /run/acp-agent-vm must exist" >&2
    exit 2
fi

if [ -e /.flatpak-info ]; then
    echo "Run this in a host terminal, not inside a Flatpak." >&2
    exit 1
fi

src=$(cd "$(dirname "$0")" && pwd)
podman="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"

if [ "$build" = 1 ]; then
    for agent in codex claude; do
        echo "== Building localhost/$agent-agent:local"
        (cd "$src/$agent-container" && $podman build \
            --build-context office-mcp="$src/office-mcp" --build-context univer-cli="$src/univer-cli" \
            -t "localhost/$agent-agent:local" .)
    done
fi

echo "== Installing connectors"
if [ "$agent_vm" = 1 ]; then
    "$src/acp-connector/install.sh" --agent-vm
else
    "$src/acp-connector/install.sh"
fi

echo "== Enabling sockets"
systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket

if [ "$test" = 1 ]; then
    echo "== Smoke tests"
    "$src/acp-connector/tests/host-smoke-test.py" codex
    "$src/acp-connector/tests/host-smoke-test.py" claude
    # The Teams profiles are optional: tested only where they're enabled.
    for profile in codex-teams claude-teams; do
        if systemctl --user -q is-enabled "$profile-acp-connector.socket" 2>/dev/null; then
            "$src/acp-connector/tests/host-smoke-test.py" "$profile"
        fi
    done
fi

echo "Done. First use of each agent: log in as described in codex-container/README.md and claude-container/README.md."
