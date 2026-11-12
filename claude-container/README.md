# Claude Code container for rootless Podman

The Claude Code counterpart of `../codex-container`. The connector in
`../acp-connector/` starts one container per Zed connection, which all
Claude threads on a project share, with all of `~/Git`
read-write. See that README for setup, the
ACP policy, tests and limitations.

## Image

- The same pinned Node 22 base, Debian packages (build tools, Python venv,
  OpenJDK 17, OpenSSH client, `flatpak-builder`, `rpm`, Chromium) and `PATH` setup as
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
- The Word and Excel MCP servers from [`../office-mcp`](../office-mcp/README.md),
  in venvs under `/opt/office-mcp`, registered in the seeded `.claude.json`.
- The Univer Workspace CLI `uvw` from [`../univer-cli`](../univer-cli/README.md),
  in `/opt/univer-workspace-cli`, with its Skill in
  `/etc/claude-code/.claude/skills`. Its login is kept in this agent's state
  volume (`UNIVER_HOME`).
- OpenCode, from the same lock as `../opencode-container`, with the
  `opencode-delegate` wrapper and the `delegate-writing` Skill: Claude can
  hand writing to a cheaper model on OpenCode Go and review the result. See
  [Delegating writing to OpenCode Go](#delegating-writing-to-opencode-go).

## Build (host terminal)

```sh
cd ~/Git/zed-agent-containers/claude-container
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs \
  build --build-context office-mcp=../office-mcp --build-context univer-cli=../univer-cli \
  --build-context opencode-cli=../opencode-container \
  -t localhost/claude-agent:local .
```

## State and login

The `claude-agent-state` volume is mounted at `/home/node/.claude`, and
`CLAUDE_CONFIG_DIR` points there, so `.claude.json` (account, trust) lives
in the volume too. Testing showed Claude writes only there and to `/tmp`,
never to the read-only `HOME`. Your host `~/.claude` is not mounted. Log in
with a one-off container using the broker's user mapping, so it can read
and update the private credential file in the existing volume:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm -it \
  --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli auth login --claudeai      # or --console
```

Check that the login persists in a fresh container:

```sh
podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs run --rm \
  --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli auth status
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
  --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli mcp add-json --scope user rea \
  '{"type":"stdio","command":"ssh","args":["-T","-F","/home/node/.ssh/agent-vm.conf","agent-vm","/usr/local/bin/rea","mcp"],"timeout":1200000}'
```

The seeded `.claude.json` also registers the Word and Excel MCP servers,
`docx-mcp` and `excel` (see [`../office-mcp`](../office-mcp/README.md)).
They run inside the container. To add them to an existing volume after
rebuilding, run this from a host terminal, then restart the Claude agent:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
$P run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli mcp add-json --scope user docx-mcp \
  '{"type":"stdio","command":"/opt/office-mcp/docx/bin/docx-mcp-server","args":[]}'
$P run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli mcp add-json --scope user excel \
  '{"type":"stdio","command":"/opt/office-mcp/excel/bin/excel-mcp-server","args":["stdio"]}'
```

## Delegating writing to OpenCode Go

Claude can hand implementation work to a cheaper model on
[OpenCode Go](https://opencode.ai/go) (default
`opencode-go/deepseek-v4-pro`) and keep orchestrating and reviewing. The
`delegate-writing` Skill (`delegate-writing-SKILL.md`, installed in
`/etc/claude-code/.claude/skills`) tells Claude when to do this:

- only when writing the spec and reviewing the diff costs it fewer tokens
  than writing the change itself; small fixes it just makes itself;
- in a git worktree, so the worker's changes stay separate from yours;
- with Claude running the checks itself, fixing small issues directly and
  sending larger ones back to the same OpenCode session.

You can also ask for delegation, or tell Claude not to, in any thread.

`opencode-delegate` (`opencode-delegate.py`) runs `opencode run` headless in
the current directory with every tool allowed, writes the full transcript
to `~/PROJECT/tmp/opencode-delegate/`, and prints only the session ID, the
worker's short report, cost and the git summary. It accepts only
`opencode-go/` models (`-m`, or `OPENCODE_DELEGATE_MODEL`). The worker
runs in the same container as Claude, with the same access. Its
OpenCode config, `/etc/opencode/opencode.json` (from `opencode.json` here),
loads no MCP servers and gives it the container notes (`CLAUDE.md`) and
`opencode-delegate.md`: stay in scope, don't commit, report briefly.
OpenCode's per-user config in the shared home (`~/.config/opencode`) and the
repository's `AGENTS.md` still apply.

OpenCode's data (the Go login, sessions and logs) is kept in this agent's
state volume, in `opencode-data`, not in `opencode-agent-state`, so Claude
never sees your other OpenCode logins. Log in once per state volume, with
the API key from your OpenCode Go dashboard. `claude` and `claude-teams`
share `claude-agent-state`; `claude-b` uses `claude-b-agent-state`:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
for volume in claude-agent-state claude-b-agent-state; do
  $P run --rm -it --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
    -v $volume:/home/node/.claude localhost/claude-agent:local opencode-delegate login
done
$P run --rm --userns=keep-id:uid=1000,gid=1000 --user=1000:1000 \
  -v claude-agent-state:/home/node/.claude localhost/claude-agent:local opencode-delegate models
```

`opencode-delegate status` lists the stored login. The OpenCode version
moves with `../opencode-container` when `../update.sh` runs.
