# Claude Code container for rootless Podman

The Claude Code counterpart of `../codex-container`. The connector in
`../acp-connector/` starts one container per Zed connection, which all
Claude threads on a project share, with `~/Git` read-only and the open
project read-write. See that README for setup, the
ACP policy, tests and limitations.

## Image

- The same pinned Node 22 base, Debian packages (build tools, Python venv,
  OpenJDK 17, OpenSSH client) and `PATH` setup as `codex-container`. The
  package layer is shared between the two images.
- `@agentclientprotocol/claude-agent-acp` 0.81.2. The Claude Code CLI
  (2.1.280) is the locked native binary
  of `@anthropic-ai/claude-agent-sdk` 0.3.280, installed without lifecycle
  scripts.
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
