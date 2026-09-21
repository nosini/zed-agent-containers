# Zed (Flatpak) ↔ per-project agent containers: ACP connector

Codex (`../codex-container`) and Claude Code (`../claude-container`) run in
rootless Podman containers. When you open an agent thread on a project in
Zed, that agent gets:

| | Access |
|---|---|
| the project the thread was opened on | read-write, including `.git` |
| everything else in `~/Git` | read-only |
| `~/Android/Sdk` | read-only |
| the rest of your home (`~/.ssh`, `~/.config`, …), D-Bus, Podman socket | none |
| its own state volume (login, settings, history) | read-write |
| the container's home `/home/node` (shared `agent-home` volume) | read-write, persistent |
| editor callbacks (make Zed read/write files, run terminals) | blocked |
| network | open |

This lets Zed itself have broad file access, so you can edit your SSH
config and other files, while the agents only see `~/Git`. It needs no
Flatpak host escape.

```
Zed Flatpak (cwd = project)          host (systemd --user)                       per connection
acp-client.py                   ──►  $XDG_RUNTIME_DIR/app/dev.zed.Zed/       ──►  podman run --rm -i
 ACP_CONNECTOR=codex|claude           codex-acp.sock  → acp-broker --profile codex   ~/Git:ro, <project>:rw,
 (stdio ↔ socket, no policy)          claude-acp.sock → acp-broker --profile claude  agent-home, <agent>-agent-state
```

## How the project is chosen

Zed starts agent servers with the project root as their working directory.
The broker reads the connecting client's directory from
`/proc/<peer pid>/cwd` and accepts it only if all of these hold:

- it is strictly inside `~/Git`;
- it is its own real path (no symlinks);
- it is the **same directory** as seen from the host (same device and inode);
- it contains no characters that could alter Podman's `--mount` syntax.

Otherwise the connection is refused with an error shown in Zed, e.g. "Open a
project inside ~/Git". Sessions must then stay inside that project. The
client never sends a path; it can only choose which of the two agents to
talk to.

Each connection gets a fresh `podman run --rm` container: the fixed image,
adapter and flags, with only the project path varying.
- **Flags:** read-only root filesystem, tmpfs `/tmp`, `--cap-drop=all`,
  `no-new-privileges`, `keep-id` (files it creates are owned by you), PID
  limit 4096, `--log-driver=none` (ACP traffic isn't copied to container
  logs), and SELinux labels disabled. There are no memory or CPU limits, by
  choice: builds can use the whole machine, as they did when the agents ran
  in Zed.
- **Lifecycle:** the container is removed when the thread's connection
  ends. The broker also removes leftovers from a crash when it starts.

## Building and testing

The agents run commands inside their container.
- **In the images:** Node 22 + npm, Python 3.11 with `venv`/`pip`,
  `build-essential`, `pkg-config`, `libssl-dev`, OpenJDK 17, git, curl and
  archive tools.
- **Toolchains the agents install themselves:** anything else goes into the
  shared, persistent `agent-home` volume, without root. That covers
  `rustup`, `uv` (other Python versions), the Go tarball in `~/.local/go`,
  and `npm -g` (prefix `~/.local`). Their bin directories are on `PATH`, and
  build caches (npm, Gradle, cargo, Go) persist there too.
- **Android:** your `~/Android/Sdk` is mounted read-only at the same path,
  with `ANDROID_HOME` set, so projects' `local.properties` works unchanged.
  A missing SDK component has to be installed on the host. The containers
  use their own `~/.gradle`, not yours, whose `gradle.properties` may hold
  credentials.
- **Environment note:** `~/.config/acp-connector/agent-environment.md` is
  mounted read-only as `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`, so
  the agents know all this and don't try `sudo apt`. Edit it as you like.
- **Not possible:** containers inside the container (Podman/Docker).
- **`/tmp` is in RAM:** large build output belongs in the project or the
  agent home.
- **Reset the agents' environment:** `podman volume rm agent-home`. It's
  recreated empty from the image; logins stay in the state volumes.

## Git inside the containers

`install.sh` creates `~/.config/acp-connector/gitconfig` from your host git
config: name, email, SSH signing and `commit`/`tag.gpgsign`. The signing
public key is embedded as `key::…`, so no `~/.ssh` path is mounted. The file
is mounted read-only as the containers' global git config.

**Signing:** if `install.sh` finds Bitwarden's SSH agent socket, the broker
forwards it into each container (`SSH_AUTH_SOCK`). Agent commits are then
signed with your key after you approve the request in Bitwarden.
- If Bitwarden is closed or locked, signed commits fail with a clear error.
- The socket also allows SSH login and pushing, each only with your
  approval. Pushing would additionally need `known_hosts`, which isn't
  mounted.
- A container keeps the socket it started with. If Bitwarden restarts,
  open a new thread to reconnect.
- To turn forwarding off, remove `--ssh-agent-socket …` from
  `~/.config/acp-connector/broker.env`.

**Git worktrees:** a worktree whose main repository is another folder in
`~/Git` can't commit from the container, because that folder is read-only.

## ACP policy (both agents, enforced on every message)

| Direction | Allowed | Everything else |
|---|---|---|
| agent → Zed requests | `session/request_permission` (filtered) | Answered by the broker with `-32601` and never shown to Zed: `fs/*`, `terminal/*`, `mcp/*`, `elicitation/*`, `_…` extensions, unknown methods |
| agent → Zed notifications | `session/update` with standard kinds | Dropped |
| Zed → agent | initialize, authenticate, logout, `session/*`, `$/cancel_request` | `-32601`/dropped: `nes/*`, `document/*`, `providers/*`, `mcp/*`, extensions |
| responses | Only if they match a request that was forwarded and is still pending | Dropped |

Rewrites:
- **Capabilities, both directions:** the client capabilities become "no fs,
  no terminal", so the agents use their own tools inside the container.
  Login methods that make Zed run a command (`terminal-auth`,
  `terminal`/`env_var`) are removed, and MCP is advertised as off.
- **Opening sessions:** forwarded MCP servers and `additionalDirectories`
  are removed, and `cwd` must be inside the connection's project.
- **Tool calls and permission prompts:** file locations and diffs are only
  shown if they are inside `~/Git`, and are forwarded normalized. Terminal
  content is never shown.
- **`_meta`** is removed at every depth, in both directions. Every message
  is re-encoded from the parsed object.
- **Logs** contain method names and decisions only:
  `journalctl --user -u codex-acp-connector` or `-u claude-acp-connector`.

These editor restrictions matter more now than before: Zed may have broad
file access, and an agent must not be able to use it.

## Setup (from a host terminal)

Rebuild both images. They share the build-tools layer, so the second build
is faster:

```sh
P="podman --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs"
(cd ~/Git/zed-agent-containers/codex-container && $P build -t localhost/codex-agent:local .)
(cd ~/Git/zed-agent-containers/claude-container && $P build -t localhost/claude-agent:local .)
```

Install. This also migrates the earlier long-lived containers; their state
volumes and logins are kept:

```sh
cd ~/Git/zed-agent-containers/acp-connector
./install.sh            # check the gitconfig, broker.env and agent-environment.md it reports
systemctl --user enable --now codex-acp-connector.socket claude-acp-connector.socket
tests/host-smoke-test.py codex && tests/host-smoke-test.py claude
```

If you still need to log in, use a one-off container with the state volume:

```sh
$P run --rm -it -v codex-agent-state:/home/node/.codex localhost/codex-agent:local codex login --device-auth
$P run --rm -it -v claude-agent-state:/home/node/.claude localhost/claude-agent:local \
  claude-agent-acp --cli auth login --claudeai
```

Zed entries: see `zed-settings.jsonc` (already added to your settings).

**Then, in this order:**
1. Remove Zed's unrestricted agents: the registry `codex-acp` and
   `claude-acp` entries, and don't use Zed's built-in agent. They run with
   Zed's own permissions.
2. Loosen Zed's filesystem permissions in Flatseal as you like.
3. Optionally turn off host escape:
   `flatpak override --user --no-talk-name=org.freedesktop.Flatpak --env=ZED_FLATPAK_NO_ESCAPE=1 dev.zed.Zed`

**Another git root:** change `--git-root %h/Git` in both
`*-acp-connector.service` files and rerun `install.sh`.

## Tested guarantees

`python3 -m unittest -v tests/test_connector.py` (or
`python3 tests/test_connector.py -v`) runs 20 tests. They pass inside Zed's
Flatpak without Podman or host escape, and inside the agent containers
(Python 3.11). The two real-adapter tests skip when that adapter isn't
installed; each image has only its own. A fake Zed starts the
**real client** in a project directory, and it connects over the real
socket to the broker:

- **Project selection:**
  - The project is the client's working directory; a symlink inside the
    git root resolves to the real project.
  - Refused: `~/Git` itself, directories outside it, a symlink from inside
    `~/Git` to outside, paths with a comma, and `/`. No agent starts for a
    refused project.
  - A session in a sibling project is rejected.
- **Container command:** for both profiles, the exact `podman run` flags
  and mount set are checked: `~/Git` ro, project rw, agent home, state
  volume, and the optional gitconfig, Android SDK and environment note (all
  ro) and agent socket. A missing or unknown profile, and a bad SDK or note
  path, are refused.
- **Editor callbacks:** a hostile mock agent tries writes, reads of
  `~/.codex/auth.json`, terminals, MCP, elicitation, extensions,
  `terminal-auth` login, forged, duplicate-key and malformed messages. None
  reach the editor and no file is written. Locations outside `~/Git` are
  removed.
- **Capability rewriting** is checked from both ends. MCP servers and extra
  directories are stripped, and client-side `nes/*`, `document/*` and
  extensions don't reach the agent.
- **Lifecycle:** disconnect stops the agent. An agent that ignores EOF and
  SIGTERM is killed, and its container removal is triggered. A crash is
  reported. There are at most 4 sessions per agent. An oversized message
  closes only its own connection. Stale and non-socket paths are handled,
  and sockets are 0600.
- **Socket activation:** the broker does not unlink the systemd-owned
  socket path.
- **Real adapters, offline, throwaway state:** codex-acp 1.12.0 and
  claude-agent-acp 0.79.0 both complete the handshake through the policy.
- **Mutation checks:** disabling the git-root check, the path-character
  check, the read-only flag on `~/Git`, or the session-directory check each
  makes the suite fail.

## Not tested here / remaining limitations

- **Verified on the host** (`tests/host-smoke-test.py`, both agents, all
  passing): images with build tools and Java, per-connection containers
  with the expected mounts, project rw / rest of `~/Git` ro, writable agent
  home, no host home, git identity, SSH agent forwarding, refusal of
  `~/Git` itself, and container removal. Signing through Bitwarden was
  verified by hand (`git verify-commit`). Actual Android/Gradle, Rust or Go
  builds have not been tried yet.
  `tests/host-smoke-test.py` checks all of these. If reading the client's
  directory fails on your host, connections are refused; they never fall
  back to something broader.
- **Starting a thread takes about 1–3 s longer,** because each connection
  starts a container.
- **The agents can change anything in the open project,** including `.git`
  (hooks, config) and `.zed/settings.json`, which Zed acts on. They can read
  every repo in `~/Git`, including any secrets in them, and the network is
  open. This fits a "not malicious, maybe careless" threat model; review
  `git diff` as usual.
- **Displayed content is agent-controlled:** messages, links and diffs.
- **UX cost of failing closed:** command output isn't streamed into tool
  cards. Unsaved buffers aren't visible, since agents read from disk. There
  are no MCP servers and no login from Zed's panel. Agent extensions are
  dropped.
- **Same-user host processes** can use the sockets; they could run `podman`
  anyway.
- **SELinux:** labels are disabled for the containers. `:z` relabelling of
  all of `~/Git` would be invasive, and a `udica` policy is the stronger
  option if you want SELinux confinement back.
