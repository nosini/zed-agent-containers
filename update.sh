#!/bin/sh
# Update the agent images: move the pinned npm packages and the Node base
# image digest to their latest releases, then rebuild both images without
# cache so the Debian packages are refreshed too. Run from a HOST terminal.
#   ./update.sh             bump the pins, rebuild, run the smoke tests
#   ./update.sh --check     only show pinned and latest versions
#   ./update.sh --no-build  only bump the pins
# Nothing is committed: review `git diff` and commit the new pins. With
# nothing new to pin, this still rebuilds, which refreshes the Debian
# packages. Open threads keep their container; new threads use the new images.
set -eu

check=0 build=1
for arg in "$@"; do
    case $arg in
        --check) check=1 ;;
        --no-build) build=0 ;;
        *) echo "usage: $0 [--check] [--no-build]" >&2; exit 2 ;;
    esac
done

if [ -e /.flatpak-info ]; then
    echo "Run this in a host terminal, not inside a Flatpak." >&2
    exit 1
fi

src=$(cd "$(dirname "$0")" && pwd)
podman="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
base=docker.io/library/node:22-bookworm-slim

# npm on the host if there is one, else in the pinned base image.
npm_in() {
    npm_dir=$1; shift
    if command -v npm >/dev/null 2>&1; then
        (cd "$npm_dir" && npm "$@")
    else
        image=$(sed -n 's/^FROM //p' "$src/codex-container/Containerfile")
        $podman run --rm --userns=keep-id --security-opt=label=disable -e HOME=/tmp \
            -v "$npm_dir:/w" -w /w "$image" npm "$@"
    fi
}

pinned() {  # pinned <dir> <package>
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"][sys.argv[2]])' \
        "$src/$1/package.json" "$2"
}

latest() {
    npm_in "$src" view "$1" version
}

codex=$(latest @openai/codex)
codex_acp=$(latest @agentclientprotocol/codex-acp)
claude_acp=$(latest @agentclientprotocol/claude-agent-acp)
$podman pull -q "$base" >/dev/null
digest=$($podman image inspect --format '{{.Digest}}' "$base")
case $digest in
    sha256:*) ;;
    *) echo "unexpected digest for $base: $digest" >&2; exit 1 ;;
esac
current=$(sed -n "s|^FROM $base@||p" "$src/codex-container/Containerfile")

echo "Package                               pinned        latest"
printf '%-37s %-13s %s\n' @openai/codex "$(pinned codex-container @openai/codex)" "$codex" \
    @agentclientprotocol/codex-acp "$(pinned codex-container @agentclientprotocol/codex-acp)" "$codex_acp" \
    @agentclientprotocol/claude-agent-acp "$(pinned claude-container @agentclientprotocol/claude-agent-acp)" "$claude_acp"
echo "$base"
echo "  pinned $current"
echo "  latest $digest"
[ "$check" = 0 ] || exit 0

# codex-acp declares a narrow @openai/codex range; the override pins the
# latest Codex anyway, as before.
range=$(npm_in "$src" view "@agentclientprotocol/codex-acp@$codex_acp" dependencies.@openai/codex)
echo "Note: codex-acp $codex_acp declares @openai/codex $range; pinning $codex through overrides."

echo "== Updating pins"
npm_in "$src/codex-container" pkg set "dependencies.@openai/codex=$codex" \
    "overrides.@openai/codex=$codex" "dependencies.@agentclientprotocol/codex-acp=$codex_acp"
npm_in "$src/claude-container" pkg set "dependencies.@agentclientprotocol/claude-agent-acp=$claude_acp"
for image_dir in codex-container claude-container; do
    # Pins first, then the other dependencies within their ranges.
    npm_in "$src/$image_dir" install --package-lock-only --ignore-scripts --no-audit --no-fund
    npm_in "$src/$image_dir" update --package-lock-only --ignore-scripts --no-audit --no-fund
    sed -i "s|^FROM $base@sha256:[0-9a-f]*|FROM $base@$digest|" "$src/$image_dir/Containerfile"
done
git -C "$src" diff --stat

if [ "$build" = 1 ]; then
    # Only the first build skips the cache: the second then reuses its fresh
    # Debian layer, which keeps that layer shared between the images.
    echo "== Building localhost/codex-agent:local"
    (cd "$src/codex-container" && $podman build --no-cache -t localhost/codex-agent:local .)
    echo "== Building localhost/claude-agent:local"
    (cd "$src/claude-container" && $podman build -t localhost/claude-agent:local .)
    echo "== Smoke tests"
    "$src/acp-connector/tests/host-smoke-test.py" codex
    "$src/acp-connector/tests/host-smoke-test.py" claude
    echo "Old images: $podman image prune"
fi
echo "Review and commit: git -C $src diff"
