# Codex container for rootless Podman

A locally built image for Codex. The connector in `../acp-connector/`
starts one container per Zed connection, which all Codex threads on a
project share, with `~/Git` read-only and the open project read-write. See that README for setup, the ACP policy, tests and
limitations.

## Image

- Official `docker.io/library/node:22-bookworm-slim`, pinned by manifest digest.
- `@openai/codex` 0.157.1 and `@agentclientprotocol/codex-acp` 1.13.1,
  with dependencies and integrity hashes in `package-lock.json`. npm
  lifecycle scripts are disabled during installation.
- Debian packages: Git, OpenSSH client (for SSH commit signing), ripgrep,
  curl, archive tools, `build-essential`, `pkg-config`, `libssl-dev`,
  Python 3 with `venv`/`pip`/headers, and OpenJDK 17. Their versions are not
  frozen, so uncached rebuilds (`../update.sh`) pick up security updates. This step is
  identical in the Claude image, so the layer is shared.
- `PATH` puts the pinned adapter first, then toolchains the agents install
  into their writable home (`~/.local/bin`, `~/.cargo/bin`, `~/.local/go/bin`,
  `~/go/bin`). `npm -g` installs into `~/.local`, and `JAVA_HOME` is set.
- The runtime user is UID/GID 1000, mapped to your host user by rootless
  `keep-id`.
- `/opt/agent-git/` holds mount targets for the connector's gitconfig
  (`GIT_CONFIG_GLOBAL`) and SSH agent socket.

Upstream adapter: https://github.com/agentclientprotocol/codex-acp

## Build (host terminal)

```sh
cd ~/Git/zed-agent-containers/codex-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build -t localhost/codex-agent:local .
```

The explicit storage option works around this host's unavailable kernel
OverlayFS without editing the global storage configuration. The connector
uses the same option.

## State and login

The `codex-agent-state` volume (`codex-agent-state.volume`, installed by
the connector) is mounted at `/home/node/.codex`. It holds the
configuration, your login and history. It is initialized from the image on
first use; rebuilding does not overwrite an existing volume. Log in with a
one-off container:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm -it \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local codex login --device-auth
```

`config.toml` (container-only) disables Codex's inner sandbox
(`danger-full-access`, `approval_policy = "never"`), because Podman provides
the isolation. The container still can't write outside the open project
and its state.
