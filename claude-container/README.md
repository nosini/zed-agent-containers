# Claude Code container for rootless Podman

The Claude Code counterpart of `../codex-container`. The connector in
`../acp-connector/` starts one container per Zed connection, which all
Claude threads on a project share, with `~/Git` read-only and the open
project read-write. See that README for setup, the
ACP policy, tests and limitations.

## Image

- The same pinned Node 22 base, Debian packages (build tools, Python venv,
  OpenJDK 17, OpenSSH client, `flatpak-builder`, `rpm`) and `PATH` setup as
  `codex-container`. The package layer is shared between the two images.
- `@agentclientprotocol/claude-agent-acp` 0.88.0. The Claude Code CLI
  (2.1.293) is the locked native binary
  of `@anthropic-ai/claude-agent-sdk` 0.3.293, installed without lifecycle
  scripts. This version supports Claude Haiku 5.5 (`claude-haiku-5-5`).
- The auto-updater is off, and the image filesystem is read-only anyway. To
  update, run `../update.sh`. The adapter pins the SDK, and so the CLI,
  exactly, so bumping the adapter updates both.
- `DISABLE_TELEMETRY=1` turns off Claude Code's usage telemetry (the
  Datadog log intake). It's set as an image `ENV`, not in `settings.json`,
  because the state volume keeps whatever `settings.json` it was created
  with.
- `settings.json` (container-only) sets `permissions.defaultMode` to
  `bypassPermissions`, matching Codex's `approval_policy = "never"`. Bypass
  is allowed because the container runs as uid 1000, not root. You can
  switch modes per session in Zed.
- `/opt/agent-git/` holds mount targets for the connector's gitconfig and
  SSH agent socket.

## Build (host terminal)

```sh
cd ~/Git/zed-agent-containers/claude-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build -t localhost/claude-agent:local .
```

## State and login

The `claude-agent-state` volume is mounted at `/home/node/.claude`, and
`CLAUDE_CONFIG_DIR` points there, so `.claude.json` (account, trust) lives
in the volume too. Testing showed Claude writes only there and to `/tmp`,
never to the read-only `HOME`. Your host `~/.claude` is not mounted. Log in
with a one-off container:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm -it \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli auth login --claudeai      # or --console
```

Login isn't possible from Zed's panel. Every login method the adapter
offers makes Zed run a command, and the connector blocks that.

Claude Code applies project `.claude/settings.json` (including hooks) and
`.mcp.json` from the open repo. They run inside the container.

The image seeds `.claude.json` with a user-wide REA MCP server over SSH
stdio. It runs `/usr/local/bin/rea mcp` on the VM, using the shared REA,
Ghidra and Java installation described in [the Codex README](../codex-container/README.md).
The selected executable must be present on the VM, and the connector must
mount the VM activation sockets. `MCP_TIMEOUT=600000` allows ten minutes
for startup, including a VM cold boot; REA's tool timeout is twenty minutes.

Rebuilding seeds new state volumes only. For an existing volume, merge the
`mcpServers.rea` entry from `claude.json` into
`/home/node/.claude/.claude.json`, preserving account and trust settings,
then restart the Claude agent. You can register the entry from a host
terminal after rebuilding:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli mcp add-json --scope user rea \
  '{"type":"stdio","command":"ssh","args":["-T","-F","/home/node/.ssh/agent-vm.conf","agent-vm","/usr/local/bin/rea","mcp"],"timeout":1200000}'
```
