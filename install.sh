#!/bin/sh
# Full setup: build the agent images, install the connectors, enable the
# socket units and run the smoke tests. Run from a HOST terminal (not in Zed).
#   ./install.sh               everything
#   ./install.sh --no-build    skip the image builds (after editing only the broker)
#   ./install.sh --no-test     skip the smoke tests
# Safe to re-run; acp-connector/install.sh alone only reinstalls the host files.
set -eu

build=1 test=1
for arg in "$@"; do
    case $arg in
        --no-build) build=0 ;;
        --no-test) test=0 ;;
        *) echo "usage: $0 [--no-build] [--no-test]" >&2; exit 2 ;;
    esac
done

if [ -e /.flatpak-info ]; then
    echo "Run this in a host terminal, not inside a Flatpak." >&2
    exit 1
fi

src=$(cd "$(dirname "$0")" && pwd)
podman="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"

if [ "$build" = 1 ]; then
    for agent in codex claude zcode; do
        echo "== Building localhost/$agent-agent:local"
        (cd "$src/$agent-container" && $podman build -t "localhost/$agent-agent:local" .)
    done
fi

echo "== Installing connectors"
"$src/acp-connector/install.sh"

echo "== Enabling sockets"
systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket zcode-acp-connector.socket

if [ "$test" = 1 ]; then
    echo "== Smoke tests"
    "$src/acp-connector/tests/host-smoke-test.py" codex
    "$src/acp-connector/tests/host-smoke-test.py" claude
    "$src/acp-connector/tests/host-smoke-test.py" zcode
fi

echo "Done. Configure each agent as described in its container README."
