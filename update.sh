#!/bin/sh
# Update the agent images: move the pinned npm packages and the Word and
# Excel MCP servers (PyPI) to their newest releases that are at least 3 days
# old, and the Node base image digest to the latest, then rebuild both
# images without cache so the Debian packages are refreshed too. Run from a
# HOST terminal, after a first ./install.sh: the MCP servers are locked with
# the Python in the Codex image.
#   ./update.sh               bump the pins, rebuild, run the smoke tests
#   ./update.sh --check       only show pinned, latest and chosen versions
#   ./update.sh --no-build    only bump the pins
#   ./update.sh --min-age=N   releases must be N days old (0: take the latest)
# The waiting period skips releases too new to have been checked by anyone,
# which is how compromised npm releases are usually caught. It applies to
# the other npm and Python dependencies too (npm --before, pip
# --uploaded-prior-to). Pins are never downgraded.
# The base image and Debian packages are not delayed: they carry security
# fixes. Nothing is committed: review `git diff` and commit the new pins. With
# nothing new to pin, this still rebuilds, which refreshes the Debian
# packages. Zed runs all of an agent's threads over one connection, and so
# one container: they only get the new image once that connection ends
# (restart the broker or Zed; the script prints the command).
set -eu

step="checking versions"
trap 'status=$?; [ "$status" = 0 ] || echo "update.sh: failed while $step (exit $status)" >&2' EXIT

check=0 build=1 min_age=3
for arg in "$@"; do
    case $arg in
        --check) check=1 ;;
        --no-build) build=0 ;;
        --min-age=*[!0-9]*|--min-age=) echo "--min-age takes a whole number of days" >&2; exit 2 ;;
        --min-age=*) min_age=${arg#--min-age=} ;;
        *) echo "usage: $0 [--check] [--no-build] [--min-age=DAYS]" >&2; exit 2 ;;
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

cutoff=$(python3 -c 'import datetime, sys
t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=int(sys.argv[1]))
print(t.strftime("%Y-%m-%dT%H:%M:%S.000Z"))' "$min_age")

# pick <package> <pinned>: prints "chosen latest published". Chosen is the
# newest stable release published before the cutoff, or the pin if that is
# newer (never a downgrade); published is when the chosen version came out.
pick() {
    npm_in "$src" view "$1" time dist-tags.latest --json | python3 -c 'import json, sys
data = json.load(sys.stdin)
cutoff, pinned = sys.argv[1], sys.argv[2]
times, latest = data["time"], data["dist-tags.latest"]
def key(v):
    return tuple(int(p) for p in v.split("."))
def stable(v):
    return v.count(".") == 2 and all(p.isdigit() for p in v.split("."))
ok = [v for v, t in times.items() if stable(v) and t <= cutoff and key(v) <= key(latest)]
chosen = max(ok + [pinned], key=key)
print(chosen, latest, times[chosen])' "$cutoff" "$2"
}

# pypi_pinned <file> <package>: the version locked in office-mcp/<file>.
pypi_pinned() {
    sed -n "s/^$2==\([^ ]*\) .*/\1/p" "$src/office-mcp/$1"
}

# pypi_pick <package> <pinned>: as pick, from PyPI. A release counts as
# published when its last file was uploaded; yanked files are ignored.
pypi_pick() {
    python3 -I -c 'import json, sys, urllib.request
package, cutoff, pinned = sys.argv[1:]
with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=60) as r:
    data = json.load(r)
latest = data["info"]["version"]
times = {v: max(f["upload_time_iso_8601"] for f in files)
         for v, files in data["releases"].items()
         if files and not any(f["yanked"] for f in files)}
def key(v):
    return tuple(int(p) for p in v.split("."))
def stable(v):
    return all(p.isdigit() for p in v.split("."))
ok = [v for v, t in times.items() if stable(v) and t <= cutoff and key(v) <= key(latest)]
chosen = max(ok + [pinned], key=key)
print(chosen, latest, times[chosen])' "$1" "$cutoff" "$2"
}

codex_pick=$(pick @openai/codex "$(pinned codex-container @openai/codex)")
codex_acp_pick=$(pick @agentclientprotocol/codex-acp "$(pinned codex-container @agentclientprotocol/codex-acp)")
claude_acp_pick=$(pick @agentclientprotocol/claude-agent-acp \
    "$(pinned claude-container @agentclientprotocol/claude-agent-acp)")
codex=${codex_pick%% *} codex_acp=${codex_acp_pick%% *} claude_acp=${claude_acp_pick%% *}
docx_pick=$(pypi_pick docx-mcp-server "$(pypi_pinned docx.txt docx-mcp-server)")
excel_pick=$(pypi_pick excel-mcp-server "$(pypi_pinned excel.txt excel-mcp-server)")
docx=${docx_pick%% *} excel=${excel_pick%% *}
$podman pull -q "$base" >/dev/null
digest=$($podman image inspect --format '{{.Digest}}' "$base")
case $digest in
    sha256:*) ;;
    *) echo "unexpected digest for $base: $digest" >&2; exit 1 ;;
esac
current=$(sed -n "s|^FROM $base@||p" "$src/codex-container/Containerfile")

echo "Releases published before $cutoff ($min_age days ago) are eligible."
echo "Package                               pinned        latest        chosen"
row() {  # row <package> <pinned> <pick output>
    set -- "$1" "$2" $3
    printf '%-37s %-13s %-13s %s\n' "$1" "$2" "$4" "$3"
}
row @openai/codex "$(pinned codex-container @openai/codex)" "$codex_pick"
row @agentclientprotocol/codex-acp "$(pinned codex-container @agentclientprotocol/codex-acp)" "$codex_acp_pick"
row @agentclientprotocol/claude-agent-acp "$(pinned claude-container @agentclientprotocol/claude-agent-acp)" \
    "$claude_acp_pick"
row "docx-mcp-server (PyPI)" "$(pypi_pinned docx.txt docx-mcp-server)" "$docx_pick"
row "excel-mcp-server (PyPI)" "$(pypi_pinned excel.txt excel-mcp-server)" "$excel_pick"
echo "$base"
echo "  pinned $current"
echo "  latest $digest"
[ "$check" = 0 ] || exit 0
# The MCP servers are locked in the Codex image, below.
if ! $podman image exists localhost/codex-agent:local; then
    echo "localhost/codex-agent:local is missing; run ./install.sh first." >&2
    exit 1
fi

# codex-acp declares a narrow @openai/codex range; the override pins the
# latest Codex anyway, as before.
range=$(npm_in "$src" view "@agentclientprotocol/codex-acp@$codex_acp" dependencies.@openai/codex)
echo "Note: codex-acp $codex_acp declares @openai/codex $range; pinning $codex through overrides."

step="updating the pins"
echo "== Updating pins"
npm_in "$src/codex-container" pkg set "dependencies.@openai/codex=$codex" \
    "overrides.@openai/codex=$codex" "dependencies.@agentclientprotocol/codex-acp=$codex_acp"
npm_in "$src/claude-container" pkg set "dependencies.@agentclientprotocol/claude-agent-acp=$claude_acp"
# The other dependencies resolve as of the cutoff too. npm refuses pins
# newer than --before, so a pin kept from an earlier, newer choice moves
# that image's date up to when the pin was published.
later() { if [ "$1" \> "$2" ]; then echo "$1"; else echo "$2"; fi; }
codex_before=$(later "$(later "$cutoff" "${codex_pick##* }")" "${codex_acp_pick##* }")
claude_before=$(later "$cutoff" "${claude_acp_pick##* }")
for image_dir in codex-container claude-container; do
    if [ "$image_dir" = codex-container ]; then before=$codex_before; else before=$claude_before; fi
    [ "$min_age" = 0 ] && before=
    # Pins first, then the other dependencies within their ranges.
    npm_in "$src/$image_dir" install --package-lock-only --ignore-scripts --no-audit --no-fund ${before:+--before=$before}
    npm_in "$src/$image_dir" update --package-lock-only --ignore-scripts --no-audit --no-fund ${before:+--before=$before}
    sed -i "s|^FROM $base@sha256:[0-9a-f]*|FROM $base@$digest|" "$src/$image_dir/Containerfile"
done
# The MCP servers' dependencies, hash-pinned for the images' Python 3.11, by
# office-mcp/lock.py in the current Codex image (with the same user mapping
# as the broker, so it can write the lock files).
step="locking the MCP servers"
office_before=$(later "$(later "$cutoff" "${docx_pick##* }")" "${excel_pick##* }")
[ "$min_age" = 0 ] && office_before=
$podman run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 --security-opt=label=disable \
    -v "$src/office-mcp:/w" localhost/codex-agent:local \
    python3 -I /w/lock.py ${office_before:+--cutoff=$office_before} \
    "docx-mcp-server==$docx" "excel-mcp-server==$excel"
# No pager: waiting in less, or leaving it with Ctrl-C, would stop the builds.
git -C "$src" --no-pager diff --stat

if [ "$build" = 1 ]; then
    # Only the first build skips the cache: the second then reuses its fresh
    # Debian layer, which keeps that layer shared between the images.
    step="building localhost/codex-agent:local"
    echo "== Building localhost/codex-agent:local"
    (cd "$src/codex-container" && $podman build --no-cache \
        --build-context office-mcp="$src/office-mcp" -t localhost/codex-agent:local .)
    step="building localhost/claude-agent:local"
    echo "== Building localhost/claude-agent:local"
    (cd "$src/claude-container" && $podman build \
        --build-context office-mcp="$src/office-mcp" -t localhost/claude-agent:local .)
    step="running the smoke tests"
    echo "== Smoke tests"
    "$src/acp-connector/tests/host-smoke-test.py" codex
    "$src/acp-connector/tests/host-smoke-test.py" claude
    # The Teams profiles are optional: tested only where they're enabled.
    for profile in codex-teams claude-teams; do
        if systemctl --user -q is-enabled "$profile-acp-connector.socket" 2>/dev/null; then
            "$src/acp-connector/tests/host-smoke-test.py" "$profile"
        fi
    done
    echo "Images rebuilt. Open threads still run the old ones; to switch (ends open threads):"
    echo "  systemctl --user try-restart codex-acp-connector.service claude-acp-connector.service \\"
    echo "    codex-teams-acp-connector.service claude-teams-acp-connector.service"
    echo "Old images: $podman image prune"
fi
echo "Review and commit: git -C $src diff"
