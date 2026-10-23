# Codex container for rootless Podman

A locally built image for Codex. The connector in `../acp-connector/`
starts one container per Zed connection, which all Codex threads on a
project share, with all of `~/Git` read-write. See that README for setup, the ACP policy, tests and
limitations.

## Image

- Official `docker.io/library/node:22-bookworm-slim`, pinned by manifest digest.
- `@openai/codex` 0.160.1 and `@agentclientprotocol/codex-acp` 2.1.1,
  with dependencies and integrity hashes in `package-lock.json`. npm
  lifecycle scripts are disabled during installation.
- Debian packages: Git, OpenSSH client (for SSH commit signing), ripgrep,
  curl, archive tools, `build-essential`, `pkg-config`, `libssl-dev`,
  Python 3 with `venv`/`pip`/headers, OpenJDK 17, `flatpak-builder`, and
  `rpm` (for `rpmbuild`), Chromium and the Liberation fonts.
  Their versions are not frozen, so uncached rebuilds (`../update.sh`)
  pick up security updates. This step is identical in the Claude image,
  so the layer is shared.
- `PATH` puts the pinned adapter first, then toolchains the agents install
  into their writable home (`~/.local/bin`, `~/.cargo/bin`, `~/.local/go/bin`,
  `~/go/bin`). `npm -g` installs into `~/.local`, and `JAVA_HOME` is set.
- The runtime user is UID/GID 1000, mapped to your host user by rootless
  `keep-id`.
- `/opt/agent-git/` holds mount targets for the connector's gitconfig
  (`GIT_CONFIG_GLOBAL`) and SSH agent socket.
- The Word and Excel MCP servers from [`../office-mcp`](../office-mcp/README.md),
  in venvs under `/opt/office-mcp`, registered in the seeded `config.toml`.
- The Univer Workspace CLI `uvw` from [`../univer-cli`](../univer-cli/README.md),
  in `/opt/univer-workspace-cli`, with its Skill in `/etc/codex/skills`. Its
  login is kept in this agent's state volume (`UNIVER_HOME`).

Upstream adapter: https://github.com/agentclientprotocol/codex-acp

## Build (host terminal)

```sh
cd ~/Git/zed-agent-containers/codex-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build --build-context office-mcp=../office-mcp --build-context univer-cli=../univer-cli \
  -t localhost/codex-agent:local .
```

The explicit storage option works around this host's unavailable kernel
OverlayFS without editing the global storage configuration. The connector
uses the same option.

## State and login

The `codex-agent-state` volume (`codex-agent-state.volume`, installed by
the connector) is mounted at `/home/node/.codex`. It holds the
configuration, your login and history. It is initialized from the image on
first use; rebuilding does not overwrite an existing volume. Log in with a
one-off container using the broker's user mapping, so it can read and
update the private state files in the existing volume:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm -it \
  --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local codex login --device-auth
```

`config.toml` (container-only) disables Codex's inner sandbox
(`danger-full-access`, `approval_policy = "never"`), because Podman provides
the isolation. The container still can't write outside the open project
and its state.

The config also registers REA over SSH stdio through `/usr/local/bin/rea`
on the VM. This launcher runs REA from `/usr/local/lib/rea` and sets
`GHIDRA_INSTALL_DIR=/usr/local/share/ghidra` and
`JAVA_HOME=/usr/lib64/jvm/java-21-openjdk`; SSH keeps stdout available for
MCP messages. These shared installations are independent of project work
directories. The selected executable must be present on the VM for REA to
analyze it. The VM activation sockets must be mounted
by the connector, and the Codex agent must be restarted to load the tools.
Because the state volume is seeded only once, rebuilding the image does not
update an existing volume's `config.toml`; merge the `[mcp_servers.rea]`
entry from the image config into an existing volume when upgrading.

The config also registers the Word and Excel MCP servers, `docx-mcp` and
`excel` (see [`../office-mcp`](../office-mcp/README.md)), which run inside
the container. To add them to an existing volume after rebuilding, run
this from a host terminal, then restart the Codex agent:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$P run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local sh -c '
  codex mcp add docx-mcp -- /opt/office-mcp/docx/bin/python3 -m docx_mcp.server &&
  codex mcp add excel -- /opt/office-mcp/excel/bin/excel-mcp-server stdio'
```
