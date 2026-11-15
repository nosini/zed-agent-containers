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

- OpenCode from [`../opencode-container`](../opencode-container/README.md),
  with `opencode-delegate` and the `delegate-writing` Skill for handing
  larger writing tasks to DeepSeek on OpenCode Go. See
  [Delegating writing to OpenCode Go](#delegating-writing-to-opencode-go).

Upstream adapter: https://github.com/agentclientprotocol/codex-acp

## Build (host terminal)

```sh
cd ~/Git/zed-agent-containers/codex-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build --build-context office-mcp=../office-mcp --build-context univer-cli=../univer-cli \
  --build-context opencode-cli=../opencode-container --build-context delegation=../claude-container \
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

## Delegating writing to OpenCode Go

Codex can hand larger implementation tasks to DeepSeek on
[OpenCode Go](https://opencode.ai/go), using
`opencode-go/deepseek-v4.1-flash`, while Codex orchestrates and reviews.
Small fixes stay with Codex. The `delegate-writing` Skill in
`/etc/codex/skills` describes the workflow: write a short spec, run the
worker in a separate worktree, review its diff and new files, and verify
the checks. Delegation can also be requested or disabled in any thread.

The wrapper, Skill and worker instructions share their source with
`../claude-container`; the OpenCode version and lockfile come from
`../opencode-container`. Rebuilding updates the Skill even for existing
state volumes.

`opencode-delegate SPEC_FILE` runs OpenCode headless in the current
directory with all tools allowed. It prints a session ID, a short report,
token usage and a git summary; the full transcript goes under
`~/PROJECT/tmp/opencode-delegate/`. Continue a session with
`opencode-delegate -s SESSION FOLLOWUP_FILE`. Only `opencode-go/` models
are accepted (`-m`, or `OPENCODE_DELEGATE_MODEL`).

The worker has the same container access as Codex. The image's
`/etc/opencode/opencode.json` loads the container notes from
`~/.codex/AGENTS.md` and the worker instructions from
`/etc/opencode/delegate.md`, with no MCP servers configured. OpenCode's
per-user configuration in `~/.config/opencode` and the repository's
`AGENTS.md` also apply.

The Go login and sessions are stored in `$CODEX_HOME/opencode-data`,
inside `codex-agent-state`, separately from Claude and standalone
OpenCode logins. The `codex` and `codex-teams` profiles share this state.
After rebuilding, log in once from a host terminal with the API key
from the OpenCode Go dashboard:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local opencode-delegate login
$P run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v codex-agent-state:/home/node/.codex localhost/codex-agent:local opencode-delegate models
```

`opencode-delegate status` lists the stored login. Restart the Codex
agent after rebuilding to load the Skill and wrapper.
